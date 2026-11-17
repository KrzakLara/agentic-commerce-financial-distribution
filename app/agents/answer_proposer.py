"""What proposes a structured answer to a customer's question. Always untrusted.

A proposer reads verified facts through the two read-only tools and writes a JSON proposal. The bank
decides everything else (`app.services.verified_llm`, `app.services.regulated_communication`).

`DeterministicProposer` is this project's offline stand-in for a model, used in scripted mode: plain
keyword rules that read the same tools and emit the same JSON, so a proposal takes the same path
through the bank's checks with no key and no call. It knows only the phrases listed here. A question
worded differently gets `OUT_OF_SCOPE`, and the offline evaluation counts that as a false refusal.

The real model is `app.agents.llm_answer_proposer`, kept apart because it needs the optional SDK.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.integrations.agent_tools import (
    AgentToolCall,
    AgentToolGateway,
    AgentToolName,
    AgentVisibleStatus,
)

READ_ONLY_TOOLS = frozenset({AgentToolName.DISCOVER_PROVIDER, AgentToolName.GET_OFFICIAL_PRODUCT})
DETERMINISTIC_PROPOSER = "deterministic-offline"


class ProposerStatus(StrEnum):
    COMPLETED = "COMPLETED"
    UNAVAILABLE = "UNAVAILABLE"
    REFUSED = "REFUSED"
    TRUNCATED = "TRUNCATED"
    ROUND_LIMIT = "ROUND_LIMIT"
    INVALID_RESPONSE = "INVALID_RESPONSE"


@dataclass(frozen=True)
class ProposerRun:
    """One proposer run. `proposal_text` is untrusted until the bank has validated it."""

    status: ProposerStatus
    proposer: str
    proposal_text: str | None = None
    model: str | None = None
    tools_read: tuple[str, ...] = ()
    rejected_tools: tuple[str, ...] = ()
    failure_kind: str | None = None
    refusal_category: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class AnswerProposer(Protocol):
    name: str
    billable: bool  # True: each run costs money, so the bank's question budget applies

    def propose(self, request_json: str) -> ProposerRun: ...


class UnavailableProposer:
    """LLM mode was selected but cannot run. Fails closed: never a silent switch to scripted."""

    name = "unavailable"
    billable = False

    def __init__(self, failure_kind: str) -> None:
        self._failure_kind = failure_kind

    def propose(self, request_json: str) -> ProposerRun:  # noqa: ARG002 - nothing is read
        return ProposerRun(
            status=ProposerStatus.UNAVAILABLE, proposer=self.name, failure_kind=self._failure_kind
        )


def _pattern(*words: str) -> re.Pattern[str]:
    return re.compile("|".join(words), re.IGNORECASE)


_PRIVATE = _pattern(
    r"\b(my|someone|somebody|another|other|his|her|their)\b.{0,40}"
    r"\b(tax|iban|address|birth|passport|identifier|id number|application data)\b",
    r"steuer-?id",
    r"porezni broj",
    r"\boib\b",
)
_ACTION = _pattern(
    r"\b(for me|on my behalf|in my name|instead of me)\b",
    r"f[üu]r mich",
    r"in meinem namen",
    r"umjesto mene",
    r"\bza mene\b",
    r"\bmark\b.{0,40}\b(opened|approved|signed|verified)\b",
    r"\bset\b.{0,20}\bstatus\b",
)
_ADVICE = _pattern(
    r"\brecommend",
    r"\bshould i\b",
    r"\b(good|right|suitable|best)\b.{0,20}\b(for me|investment|choice|fund|product)\b",
    r"\bsuits? me\b",
    r"\bworth it\b",
    r"empfehl",
    r"\bsoll ich\b",
    r"preporu[cč]",
    r"\btrebam li\b",
    r"isplati",
)
_OUTCOME = _pattern(
    r"\b(will|would)\b.{0,40}\b(approved?|accepted?|rejected?)\b",
    r"\bam i eligible\b",
    r"\bdo i qualify\b",
    r"\bcan i qualify\b",
    r"\bwerde ich\b.{0,30}(angenommen|genehmigt)",
    r"\bho[cć]u li\b.{0,30}(odobren|prihva[cć]en)",
)
_HUMAN = _pattern(
    r"\b(a|real|speak to|talk to) (person|human)\b",
    r"\bsomeone at the bank\b",
    r"\b(staff|adviser|advisor)\b",
    r"\b(mitarbeiter|mensch)\b",
    r"\b(osob[aeu]|[cč]ovjek|savjetnik)\w*\b",
)
_TOPICS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GUARANTEE", _pattern(r"guarant", r"garant", r"zajam[cč]", r"\bjam[cč]")),
    (
        "RETURNS",
        _pattern(
            r"\breturns?\b", r"\byield", r"\binterest\b", r"rendite", r"\bzins", r"prinos", r"kamat"
        ),
    ),  # fmt: skip
    ("CAPITAL_PROTECTION", _pattern(r"capital protect", r"kapitalschutz", r"za[sš]tit\w* kapital")),
    (
        "DEPOSIT_INSURANCE",
        _pattern(r"deposit (insurance|protection)", r"einlagensicherung", r"osiguranj\w* depozit"),
    ),  # fmt: skip
    (
        "BANK_LICENSE",
        _pattern(
            r"licen[cs]",
            r"\bbafin\b",
            r"\bregulated\b",
            r"\bsupervis",
            r"lizenz",
            r"reguliran",
            r"dozvol",
        ),
    ),  # fmt: skip
    (
        "TAX_TREATMENT",
        _pattern(r"tax treatment", r"\btaxed\b", r"withholding", r"abgeltung", r"oporez"),
    ),  # fmt: skip
    ("PAST_PERFORMANCE", _pattern(r"past performance", r"historical", r"wertentwicklung")),
)
_ELIGIBILITY = _pattern(r"eligib", r"\bwho (can|may)\b", r"voraussetzung", r"uvjeti za otvaranje")
_TERMS = _pattern(r"\bconditions\b", r"\bterms\b", r"\bcancel", r"minimum deposit", r"bedingungen",
                  r"\buvjeti\b")  # fmt: skip
_STEPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CUSTOMER_SIGN_IN", _pattern(r"\bsign[- ]?in\b", r"\blog ?in\b", r"anmeld", r"prijav")),
    (
        "AGENT_DELEGATION",
        _pattern(r"delegat", r"\b(allow|authori[sz]e) (the|my) agent\b", r"vollmacht", r"ovlast"),
    ),  # fmt: skip
    ("IDENTITY_VERIFICATION", _pattern(r"identi", r"\bkyc\b", r"videoident", r"\beid\b")),
    ("TAX_INFORMATION", _pattern(r"tax (information|details|data)", r"steuer", r"porezn")),
    (
        "KNOWLEDGE_AND_EXPERIENCE",
        _pattern(
            r"knowledge",
            r"\bexperience\b",
            r"questionnaire",
            r"appropriate",
            r"kenntnis",
            r"znanj",
            r"iskustv",
        ),
    ),  # fmt: skip
    (
        "CONTRACT_SIGNING",
        _pattern(
            r"\bsign", r"contract", r"\bqes\b", r"unterschrift", r"vertrag", r"potpis", r"ugovor"
        ),
    ),  # fmt: skip
    ("CUSTOMER_CONFIRMATION", _pattern(r"\bconfirm", r"best[äa]tig", r"potvrd")),
    (
        "DEPOT_OPENING",
        _pattern(
            r"\bopen(ed|ing)?\b.{0,30}\b(account|depot)\b",
            r"\b(account|depot) opening\b",
            r"er[öo]ffn",
            r"otvaranj",
        ),
    ),  # fmt: skip
)
_DOCUMENT = _pattern(r"document", r"\bkid\b", r"price list", r"disclosure", r"dokument",
                     r"preisliste", r"cjenik")  # fmt: skip
_COMPARE = _pattern(r"\bcompare", r"\bversus\b", r"\bvs\.?\b", r"difference between", r"vergleich",
                    r"usporedi")  # fmt: skip
_FEE = _pattern(r"\bfees?\b", r"\bcosts?\b", r"\bcharges?\b", r"\bprices?\b", r"geb[üu]hr",
                r"kosten", r"naknad", r"tro[sš]k", r"cijen")  # fmt: skip
_RISK = _pattern(r"\brisk", r"risiko", r"rizik", r"\blose\b", r"verlier", r"izgub")
_AVAILABLE = _pattern(r"which products?", r"what products?", r"\bavailable\b", r"\boffer",
                      r"\bchannels?\b", r"welche produkte", r"koje proizvod",
                      r"dostupn")  # fmt: skip
_PROVIDER = _pattern(r"\bwho (is|are)\b", r"which bank", r"\bprovider\b", r"anbieter", r"tko je")
_LIMITATION = _pattern(r"limitation", r"einschr[äa]nk", r"ograni[cč]enj")
_SERVICES = _pattern(r"\bportfolio\b", r"order execution", r"execution[- ]only", r"\bbrokerage\b",
                     r"verm[öo]gensverwaltung", r"upravljanj\w* portfelj")  # fmt: skip


def _proposal(intent: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "intent": intent,
        "product_ids": [],
        "claims": [],
        "process_step": None,
        "not_established_topics": [],
        "document_id": None,
        "document_version": None,
        **fields,
    }


def _claim_from(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": fact["claim_id"],
        "product_id": fact["product_id"],
        "claim_type": fact["claim_type"],
        "field": fact["field"],
        "value": fact["value"],
        "source_id": fact["source_id"],
        "source_version": fact["source_version"],
        "source_digest": fact["source_digest"],
    }


class DeterministicProposer:
    """Keyword rules over the read-only tools: reproducible, offline and explicitly limited."""

    name = DETERMINISTIC_PROPOSER
    billable = False

    def __init__(self, gateway: AgentToolGateway) -> None:
        self._gateway = gateway

    def _read(
        self, tool: AgentToolName, arguments: dict[str, str], used: list[str]
    ) -> dict[str, Any]:
        used.append(tool.value)
        result = self._gateway.execute(AgentToolCall(tool=tool, arguments=arguments))
        return result.data if result.status is AgentVisibleStatus.OK else {}

    def propose(self, request_json: str) -> ProposerRun:
        request = json.loads(request_json)
        question = request["customer_question"]
        provider_id = request["provider_id"]
        used: list[str] = []
        provider = self._read(AgentToolName.DISCOVER_PROVIDER, {"provider_id": provider_id}, used)
        product_ids = request["product_ids"] or provider.get("product_ids", [])
        product_facts: list[dict[str, Any]] = []
        for product_id in product_ids:
            data = self._read(
                AgentToolName.GET_OFFICIAL_PRODUCT,
                {"provider_id": provider_id, "product_id": product_id},
                used,
            )
            product_facts.extend(data.get("facts", []))
        proposal = self._classify(question, product_ids, provider.get("facts", []), product_facts)
        return ProposerRun(
            status=ProposerStatus.COMPLETED,
            proposer=self.name,
            proposal_text=json.dumps(proposal),
            tools_read=tuple(used),
        )

    @staticmethod
    def _classify(
        question: str,
        product_ids: Sequence[str],
        provider_facts: Sequence[dict[str, Any]],
        facts: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        def selected(*types: str, source: Sequence[dict[str, Any]] = facts) -> dict[str, Any]:
            claims = [_claim_from(fact) for fact in source if fact["claim_type"] in types]
            return _proposal("PRODUCT_FACT", product_ids=list(product_ids), claims=claims)

        for intent, rule in (
            ("PRIVATE_DATA_REQUEST", _PRIVATE),
            ("ACTION_REQUEST", _ACTION),
            ("PERSONAL_ADVICE", _ADVICE),
            ("OUTCOME_PREDICTION", _OUTCOME),
            ("HUMAN_ASSISTANCE", _HUMAN),
        ):
            if rule.search(question):
                return _proposal(intent)
        if _SERVICES.search(question):
            return selected("SERVICE_SCOPE", source=provider_facts)
        topics = [topic for topic, rule in _TOPICS if rule.search(question)]
        if topics:
            return _proposal(
                "SOURCE_LIMIT", product_ids=list(product_ids), not_established_topics=topics
            )
        if _ELIGIBILITY.search(question):
            rules = selected("ELIGIBILITY_RULE")
            if rules["claims"]:
                return rules
            return _proposal(
                "SOURCE_LIMIT",
                product_ids=list(product_ids),
                not_established_topics=["ELIGIBILITY_RULES"],
            )
        if _COMPARE.search(question):
            return _proposal(
                "PRODUCT_COMPARISON",
                product_ids=list(product_ids),
                claims=_fees_in_common(facts),
            )
        if _DOCUMENT.search(question):
            return _document_proposal(question, product_ids, facts)
        if _TERMS.search(question):
            return selected("CONTRACT_TERM")
        if _FEE.search(question):
            return selected("FEE")
        if _RISK.search(question):
            return selected("RISK_CLASSIFICATION", "RISK_DISCLOSURE")
        for step, rule in _STEPS:
            if rule.search(question):
                return _proposal("PROCESS_STEP", process_step=step)
        if _LIMITATION.search(question):
            return selected("PRODUCT_LIMITATION")
        if _AVAILABLE.search(question):
            return selected("PRODUCT_IDENTITY", "PRODUCT_AVAILABILITY")
        if _PROVIDER.search(question):
            return selected("PROVIDER_IDENTITY", source=provider_facts)
        return _proposal("OUT_OF_SCOPE")


def _fees_in_common(facts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fees at least two products have: a fee of one product alone compares with nothing."""
    fees = [fact for fact in facts if fact["claim_type"] == "FEE"]
    holders: dict[str, set[str]] = {}
    for fee in fees:
        holders.setdefault(fee["field"], set()).add(fee["product_id"])
    return [_claim_from(fee) for fee in fees if len(holders[fee["field"]]) >= 2]


def _document_proposal(
    question: str, product_ids: Sequence[str], facts: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    documents = [fact for fact in facts if fact["claim_type"] == "DOCUMENT_VERSION"]
    lowered = question.lower()
    named = next(
        (
            document
            for document in documents
            if (document["source_id"].startswith("pli-") and "price" in lowered)
            or (document["source_id"].startswith("pli-") and "cjenik" in lowered)
            or (
                document["source_id"].startswith("kid-")
                and ("kid" in lowered or "disclos" in lowered)
            )
            or document["source_id"].lower() in lowered
        ),
        None,
    )
    if named is not None:
        return _proposal(
            "DOCUMENT_REQUEST",
            product_ids=list(product_ids),
            document_id=named["source_id"],
            document_version=named["source_version"],
        )
    return _proposal(
        "PRODUCT_FACT",
        product_ids=list(product_ids),
        claims=[_claim_from(document) for document in documents],
    )
