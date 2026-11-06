"""Verified product communication: deterministic checks before a text is released to a customer.

The rule: a text with a financial statement is released only if every checkable value in it matches
a
bank-approved, currently valid fact. Otherwise the customer gets a structured answer from approved
facts, an explicit EVIDENCE_INSUFFICIENT result, or a route to a person. The model is never asked to
verify itself.

Limits, stated plainly: this is pattern and value matching. It catches numbers, currencies, percent
signs, risk scales, listed wording, document references and unknown product ids. It cannot
understand
a paraphrase that contains none of them, such as a misleading sentence with no number in it.
High-impact
facts therefore come from `verified_answer`, a template over approved facts, and not from free text.
It also says nothing about what a third party's agent shows its user.
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.core.exceptions import AppError
from app.integrations.ports import ProviderDiscoveryPort
from app.services.bank_claims import (
    SAFE_RESPONSE,
    ApprovedClaim,
    ClaimKind,
    approved_claims,
)
from app.services.catalog_policy import product_is_current, provider_is_current


class Verdict(StrEnum):
    RELEASE = "RELEASE"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"


EVIDENCE_INSUFFICIENT_TEXT = (
    "The bank's approved information does not support that statement, so it cannot be presented as "
    "fact. You can ask for a person on the bank's customer page."
)
_MONEY = re.compile(
    r"(?:(?:EUR|€|USD|\$)\s*(\d[\d.,]*))|(?:(\d[\d.,]*)\s*(?:EUR|€|USD|\$|euros?\b|dollars?\b))",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"\d[\d.,]*\s*(?:%|percent\b|per cent\b|prozent\b)", re.IGNORECASE)
_NUMBER_WORD_AMOUNT = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|twenty|fifty|hundred)\s+"
    r"(?:euros?|dollars?|percent|per cent)\b",
    re.IGNORECASE,
)
_NO_COST = re.compile(
    r"\b(free of charge|no (?:fees?|costs?|charges?)|free account|costs? nothing|"
    r"zero (?:fees?|costs?))\b",
    re.IGNORECASE,
)
_RISK_SCALE = re.compile(r"\b(\d)\s*(?:of|/|out of)\s*7\b", re.IGNORECASE)
_DOCUMENT_REF = re.compile(
    r"\b((?:kid|pli)-[a-z0-9-]+)(?:\s+(?:version\s+|v)([0-9][0-9.]*))?", re.IGNORECASE
)
_PRODUCT_ID = re.compile(r"\bdepot-[a-z0-9-]+\b", re.IGNORECASE)
_DIGEST = re.compile(r"\b[0-9a-f]{64}\b")
_RETURN_OR_GUARANTEE = re.compile(
    r"\b(guarantee[ds]?|risk[- ]free|no risk|cannot lose|can't lose|safe investment|"
    r"capital (?:is )?protected|fixed (?:interest|return)|assured return|"
    r"will (?:definitely |certainly |surely )?(?:approve|be approved|accept|open|earn|gain|grow)|"
    r"definitely (?:approve|be approved|accept|open)|"
    r"(?:interest|yield|returns?) of\b)",
    re.IGNORECASE,
)
_ADVICE = re.compile(
    r"\b(i recommend|we recommend|recommended|recommends?\b|you should (?:buy|invest|open|choose)|"
    r"best (?:fund|product|choice|option)|right for you|suits? you|ideal for you|"
    r"good investment|perfect for you)\b",
    re.IGNORECASE,
)
_REGULATORY = re.compile(
    r"\b(bafin|licen[cs]ed|regulated|authori[sz]ed by|supervised by|compliant|certified|"
    r"mifid|deposit insurance|deposit protection|einlagensicherung|fully insured)\b",
    re.IGNORECASE,
)
_COMPARISON = re.compile(
    r"\b(cheaper|lower (?:fees?|costs?)|higher (?:fees?|costs?)|better than|worse than|"
    r"compared (?:to|with)|than (?:other|any|competitors?)|market[- ]leading)\b",
    re.IGNORECASE,
)
_INSTRUCTION = re.compile(
    r"ignore (?:all |any )?previous|disregard (?:the )?(?:above|instructions)|\bsystem\s*:|"
    r"approve_consent|set status|<script",
    re.IGNORECASE,
)
_ELIGIBILITY = re.compile(
    r"\b(you (?:are|qualify|will be) (?:eligible|approved)|you qualify|eligible for)\b",
    re.IGNORECASE,
)
_AVAILABILITY = re.compile(
    r"\b(?:savings account|credit card|crypto(?:currency)?|etf savings plan|bond fund|"
    r"gold depot)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    code: str
    excerpt: str = ""


@dataclass(frozen=True)
class GuardResult:
    verdict: Verdict
    released_text: str
    findings: tuple[Finding, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    has_financial_statement: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "released_text": self.released_text,
            "findings": [{"code": f.code, "excerpt": f.excerpt} for f in self.findings],
            "evidence": list(self.evidence),
            "has_financial_statement": self.has_financial_statement,
        }


@dataclass(frozen=True)
class _Facts:
    claims: tuple[ApprovedClaim, ...]
    fees: dict[str, str] = field(default_factory=dict)
    amounts: frozenset[str] = frozenset()
    risk_values: frozenset[str] = frozenset()
    documents: dict[str, str] = field(default_factory=dict)
    digests: frozenset[str] = frozenset()
    product_ids: frozenset[str] = frozenset()


def _normalize_number(value: str) -> str:
    value = value.strip().rstrip(".,")
    if "," in value and "." not in value:
        value = value.replace(",", ".")
    try:
        return f"{float(value.replace(',', '')):.2f}"
    except ValueError:
        return value


def _sentences(text: str) -> list[str]:
    # A point between digits belongs to the number, and one between letters to a name.
    return [part for part in re.split(r"(?<=[!?])\s+|(?<=[a-z\)])\.\s+|\n+", text) if part.strip()]


def _fee_words(code: str) -> Iterable[str]:
    return {"custody": ("custody",), "order": ("order", "trade", "transaction")}.get(code, (code,))


class CommunicationGuard:
    """Checks a text against the bank's approved facts for one provider and product."""

    def __init__(self, catalog: ProviderDiscoveryPort, clock: Callable[[], datetime]) -> None:
        self._catalog = catalog
        self._clock = clock

    def _facts(self, provider_id: str, product_id: str | None) -> tuple[_Facts | None, str | None]:
        """Current approved facts, or the reason there are none."""
        now = self._clock()
        try:
            provider = self._catalog.get_provider(provider_id)
            products = list(self._catalog.list_products(provider_id))
        except AppError:
            return None, "unknown_provider"
        if not provider_is_current(provider, now):
            return None, "source_expired"
        known = frozenset(p.product_id.lower() for p in products)
        product = next((p for p in products if p.product_id == product_id), None)
        if product_id is not None and product is None:
            return None, "unknown_product"
        if product is None:
            return _Facts(claims=(), product_ids=known), None
        if not product_is_current(product, now):
            return None, "source_expired"
        claims = tuple(approved_claims(self._catalog, provider_id, product.product_id))
        fees = {c.subject or "": c.value for c in claims if c.kind is ClaimKind.FEE}
        amounts = set()
        for value in fees.values():
            for match in _MONEY.finditer(value):
                amounts.add(_normalize_number(match.group(1) or match.group(2)))
        documents = {product.risk_document_id.lower(): product.risk_document_version}
        digests = {product.risk_document_digest}
        for source in product.sources:
            documents[source.document_id.lower()] = source.document_version
            digests.add(source.document_digest)
        risk = {m.group(1) for m in _RISK_SCALE.finditer(product.risk_class or "")}
        return (
            _Facts(
                claims=claims,
                fees=fees,
                amounts=frozenset(amounts),
                risk_values=frozenset(risk),
                documents=documents,
                digests=frozenset(digests),
                product_ids=known,
            ),
            None,
        )

    def check(self, text: str, *, provider_id: str, product_id: str | None) -> GuardResult:
        """Release, or replace with a fixed answer. Fails closed: any doubt is not released."""
        findings: list[Finding] = []
        escalate: list[Finding] = []
        financial = False

        def excerpt(match: re.Match[str]) -> str:
            return match.group(0)[:60]

        for match in _INSTRUCTION.finditer(text):
            escalate.append(Finding("instruction_like_text", excerpt(match)))
        for match in _RETURN_OR_GUARANTEE.finditer(text):
            escalate.append(Finding("guarantee_or_return_promise", excerpt(match)))
        for match in _ADVICE.finditer(text):
            escalate.append(Finding("personal_recommendation", excerpt(match)))
        for match in _REGULATORY.finditer(text):
            findings.append(Finding("regulatory_claim_without_evidence", excerpt(match)))
        for match in _COMPARISON.finditer(text):
            findings.append(Finding("unsupported_comparison", excerpt(match)))
        for match in _ELIGIBILITY.finditer(text):
            findings.append(Finding("eligibility_claim_without_evidence", excerpt(match)))
        for match in _AVAILABILITY.finditer(text):
            findings.append(Finding("unavailable_product", excerpt(match)))
        for match in _NUMBER_WORD_AMOUNT.finditer(text):
            findings.append(Finding("amount_in_words_not_verifiable", excerpt(match)))

        facts, problem = self._facts(provider_id, product_id)
        money_or_percent = bool(
            _MONEY.search(text)
            or _PERCENT.search(text)
            or _NO_COST.search(text)
            or _RISK_SCALE.search(text)
            or _DOCUMENT_REF.search(text)
            or _DIGEST.search(text)
        )
        financial = bool(money_or_percent or findings or escalate)
        if facts is None:
            if money_or_percent or findings:
                findings.append(Finding(problem or "no_facts"))
        else:
            self._check_values(text, facts, findings)
            financial = financial or any(True for _ in _PRODUCT_ID.finditer(text))

        if escalate:
            return GuardResult(
                Verdict.ESCALATE_TO_HUMAN,
                SAFE_RESPONSE,
                tuple(escalate + findings),
                has_financial_statement=True,
            )
        if findings:
            return GuardResult(
                Verdict.EVIDENCE_INSUFFICIENT,
                EVIDENCE_INSUFFICIENT_TEXT,
                tuple(findings),
                has_financial_statement=True,
            )
        evidence = (
            tuple(_evidence(claim) for claim in facts.claims if _claim_is_cited(claim, text))
            if facts
            else ()
        )
        return GuardResult(Verdict.RELEASE, text, (), evidence, has_financial_statement=financial)

    def _check_values(self, text: str, facts: _Facts, findings: list[Finding]) -> None:
        for match in _PERCENT.finditer(text):
            findings.append(Finding("percentage_without_evidence", match.group(0)[:40]))
        for match in _PRODUCT_ID.finditer(text):
            if match.group(0).lower() not in facts.product_ids:
                findings.append(Finding("unknown_product", match.group(0)[:60]))
        for match in _RISK_SCALE.finditer(text):
            if match.group(1) not in facts.risk_values:
                findings.append(Finding("risk_class_differs", match.group(0)))
        for match in _DIGEST.finditer(text):
            if match.group(0) not in facts.digests:
                findings.append(Finding("document_digest_differs", match.group(0)[:16]))
        for match in _DOCUMENT_REF.finditer(text):
            document, version = match.group(1).lower(), match.group(2)
            current = facts.documents.get(document)
            if current is None:
                findings.append(Finding("unknown_document", document[:60]))
            elif version and version.rstrip(".") != current:
                findings.append(Finding("document_version_differs", f"{document} {version}"[:60]))
        for sentence in _sentences(text):
            self._check_amounts(sentence, facts, findings)

    def _check_amounts(self, sentence: str, facts: _Facts, findings: list[Finding]) -> None:
        amounts = [_normalize_number(m.group(1) or m.group(2)) for m in _MONEY.finditer(sentence)]
        no_cost = _NO_COST.search(sentence)
        mentioned = [
            code
            for code in facts.fees
            if any(word in sentence.lower() for word in _fee_words(code))
        ]
        for amount in amounts:
            if amount not in facts.amounts:
                findings.append(Finding("amount_differs_from_approved_facts", amount))
                continue
            if mentioned and not any(_amount_of(facts.fees[code]) == amount for code in mentioned):
                # A correct number attached to the wrong fee, even with a correct citation.
                findings.append(Finding("amount_belongs_to_another_fee", amount))
        if no_cost:
            free = [code for code, value in facts.fees.items() if _amount_of(value) == "0.00"]
            if not mentioned or any(code not in free for code in mentioned):
                # "No fees" is only true fee by fee, and only where the approved value is zero.
                findings.append(Finding("no_cost_claim_differs", no_cost.group(0)[:40]))

    def verified_answer(
        self, *, provider_id: str, product_id: str, question: str, subject: str | None
    ) -> dict[str, Any]:
        """A template over approved facts: the safe way to state a high-impact fact."""
        facts, problem = self._facts(provider_id, product_id)
        base = {"provider_id": provider_id, "product_id": product_id, "question": question}
        if facts is None:
            return {
                **base,
                "result": Verdict.EVIDENCE_INSUFFICIENT.value,
                "reason": problem,
                "statement": EVIDENCE_INSUFFICIENT_TEXT,
            }
        kinds = {
            "FEE": ClaimKind.FEE,
            "RISK": ClaimKind.RISK_SUMMARY,
            "CONDITIONS": ClaimKind.CONDITIONS,
            "PROVIDER_IDENTITY": ClaimKind.PROVIDER_IDENTITY,
            "PARTNER": ClaimKind.PARTNER_ATTRIBUTION,
        }
        if question in {"GUARANTEE", "RECOMMENDATION", "APPROVAL_OUTCOME", "REGULATORY_STATUS"}:
            return {
                **base,
                "result": Verdict.ESCALATE_TO_HUMAN.value,
                "reason": "not_answerable_from_approved_facts",
                "statement": SAFE_RESPONSE,
            }
        kind = kinds.get(question)
        claim = next(
            (
                c
                for c in facts.claims
                if c.kind is kind and (subject is None or c.subject == subject)
            ),
            None,
        )
        if claim is None:
            return {
                **base,
                "result": Verdict.EVIDENCE_INSUFFICIENT.value,
                "reason": "not_in_approved_sources",
                "statement": EVIDENCE_INSUFFICIENT_TEXT,
            }
        return {
            **base,
            "result": "VERIFIED_FACT",
            "statement": claim.statement,
            "value": claim.value,
            "product_version": claim.product_version,
            "evidence": _evidence(claim),
            "nature": "Non-personalized product information. Not investment advice.",
        }

    def compare_fees(
        self, *, provider_id: str, product_ids: Sequence[str], fee_code: str
    ) -> dict[str, Any]:
        """Side by side, only if every product has an approved value. Never a ranking."""
        rows = [
            self.verified_answer(
                provider_id=provider_id, product_id=product, question="FEE", subject=fee_code
            )
            for product in product_ids
        ]
        comparable = len(rows) >= 2 and all(row["result"] == "VERIFIED_FACT" for row in rows)
        return {
            "result": "COMPARED_NOT_RANKED" if comparable else Verdict.EVIDENCE_INSUFFICIENT.value,
            "fee_code": fee_code,
            "rows": rows if comparable else [],
            "note": "Approved values side by side. This is not a recommendation."
            if comparable
            else "The approved information does not support this comparison.",
        }


def _amount_of(value: str) -> str | None:
    match = _MONEY.search(value)
    return _normalize_number(match.group(1) or match.group(2)) if match else None


def _claim_is_cited(claim: ApprovedClaim, text: str) -> bool:
    if claim.value in text:
        return True
    amount = _amount_of(claim.value) if claim.kind is ClaimKind.FEE else None
    if amount is None:
        return False
    return any(_normalize_number(m.group(1) or m.group(2)) == amount for m in _MONEY.finditer(text))


def _evidence(claim: ApprovedClaim) -> dict[str, Any]:
    return {
        "kind": claim.kind.value,
        "subject": claim.subject,
        "value": claim.value,
        "product_version": claim.product_version,
        "source": claim.source.model_dump(),
        "mock": claim.mock,
    }
