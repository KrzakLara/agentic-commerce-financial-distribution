"""The typed contract of bank-controlled communication: what a model may propose, what is released.

A model proposes a `ModelProposal`. It holds identifiers and enums, and there is no field for prose,
so a model has nowhere to put a sentence for the customer. `CommunicationGuard.release` checks the
proposal against the bank's verified facts and builds a `RegulatedAnswer`, whose statements come
only from the bank's facts and fixed templates. A value a model typed is compared with the fact and
then dropped: what reaches the customer is the fact's own value.

`proposal_json_schema` is sent to the model API as a structured-output format. It helps a real
model produce a valid proposal and enforces nothing: the bank validates the result again with these
models, whatever came back.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.base import OpaqueId

CONTRACT_VERSION = "regulated-answer/1"
NATURE = "Non-personalized product information. Not investment advice."
MAX_QUESTION_CHARS = 1000
MAX_PROPOSED_CLAIMS = 12
MAX_PRODUCTS = 10
MAX_TOPICS = 6


class ClaimType(StrEnum):
    PROVIDER_IDENTITY = "PROVIDER_IDENTITY"
    PRODUCT_IDENTITY = "PRODUCT_IDENTITY"
    PRODUCT_AVAILABILITY = "PRODUCT_AVAILABILITY"
    FEE = "FEE"
    RISK_CLASSIFICATION = "RISK_CLASSIFICATION"
    RISK_DISCLOSURE = "RISK_DISCLOSURE"
    ELIGIBILITY_RULE = "ELIGIBILITY_RULE"
    DOCUMENT_VERSION = "DOCUMENT_VERSION"
    CONTRACT_TERM = "CONTRACT_TERM"
    PRODUCT_LIMITATION = "PRODUCT_LIMITATION"
    PARTNER_ATTRIBUTION = "PARTNER_ATTRIBUTION"
    REGULATORY_STATUS = "REGULATORY_STATUS"
    SERVICE_SCOPE = "SERVICE_SCOPE"


class SupportKind(StrEnum):
    """Why a citation supports its claim. Each kind is checked when the fact is built."""

    VALUE_IN_DOCUMENT_TEXT = "VALUE_IN_DOCUMENT_TEXT"  # the cited text literally contains it
    DOCUMENT_DIGEST_VERIFIED = "DOCUMENT_DIGEST_VERIFIED"  # the text matches the signed digest
    SIGNED_MANIFEST_FIELD = "SIGNED_MANIFEST_FIELD"  # a field of the signed, pinned manifest
    BANK_POLICY_VERSION = "BANK_POLICY_VERSION"  # the bank's own versioned policy


class Intent(StrEnum):
    PRODUCT_FACT = "PRODUCT_FACT"
    PRODUCT_COMPARISON = "PRODUCT_COMPARISON"
    PROCESS_STEP = "PROCESS_STEP"
    SOURCE_LIMIT = "SOURCE_LIMIT"  # what the approved sources do not establish
    DOCUMENT_REQUEST = "DOCUMENT_REQUEST"
    HUMAN_ASSISTANCE = "HUMAN_ASSISTANCE"
    PERSONAL_ADVICE = "PERSONAL_ADVICE"
    OUTCOME_PREDICTION = "OUTCOME_PREDICTION"  # "will I be approved?"
    ACTION_REQUEST = "ACTION_REQUEST"  # "sign this for me"
    PRIVATE_DATA_REQUEST = "PRIVATE_DATA_REQUEST"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ProcessStep(StrEnum):
    AGENT_DELEGATION = "AGENT_DELEGATION"
    CUSTOMER_SIGN_IN = "CUSTOMER_SIGN_IN"
    IDENTITY_VERIFICATION = "IDENTITY_VERIFICATION"
    TAX_INFORMATION = "TAX_INFORMATION"
    KNOWLEDGE_AND_EXPERIENCE = "KNOWLEDGE_AND_EXPERIENCE"
    CUSTOMER_CONFIRMATION = "CUSTOMER_CONFIRMATION"
    CONTRACT_SIGNING = "CONTRACT_SIGNING"
    DEPOT_OPENING = "DEPOT_OPENING"


class NotEstablishedTopic(StrEnum):
    RETURNS = "RETURNS"
    GUARANTEE = "GUARANTEE"
    CAPITAL_PROTECTION = "CAPITAL_PROTECTION"
    DEPOSIT_INSURANCE = "DEPOSIT_INSURANCE"
    BANK_LICENSE = "BANK_LICENSE"
    SUITABILITY = "SUITABILITY"
    ELIGIBILITY_RULES = "ELIGIBILITY_RULES"
    TAX_TREATMENT = "TAX_TREATMENT"
    PAST_PERFORMANCE = "PAST_PERFORMANCE"


class AnswerOutcome(StrEnum):
    VERIFIED_ANSWER = "VERIFIED_ANSWER"
    COMPARED_NOT_RANKED = "COMPARED_NOT_RANKED"
    PROCESS_EXPLANATION = "PROCESS_EXPLANATION"
    NOT_ESTABLISHED_BY_SOURCES = "NOT_ESTABLISHED_BY_SOURCES"
    DOCUMENT_AVAILABLE = "DOCUMENT_AVAILABLE"
    HUMAN_ASSISTANCE_ROUTE = "HUMAN_ASSISTANCE_ROUTE"
    # Nothing but a fixed text is released with these three.
    INSUFFICIENT_VERIFIED_EVIDENCE = "INSUFFICIENT_VERIFIED_EVIDENCE"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    REQUIRES_HUMAN_REVIEW = "REQUIRES_HUMAN_REVIEW"


WITHHELD_OUTCOMES = frozenset(
    {
        AnswerOutcome.INSUFFICIENT_VERIFIED_EVIDENCE,
        AnswerOutcome.UNSUPPORTED_REQUEST,
        AnswerOutcome.REQUIRES_HUMAN_REVIEW,
    }
)


class ReasonCode(StrEnum):
    # About one proposed claim.
    UNKNOWN_CLAIM = "UNKNOWN_CLAIM"
    PRODUCT_MISMATCH = "PRODUCT_MISMATCH"
    CLAIM_TYPE_MISMATCH = "CLAIM_TYPE_MISMATCH"
    FIELD_MISMATCH = "FIELD_MISMATCH"
    VALUE_MISMATCH = "VALUE_MISMATCH"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    SOURCE_DOES_NOT_SUPPORT_CLAIM = "SOURCE_DOES_NOT_SUPPORT_CLAIM"
    SOURCE_TEXT_UNSAFE = "SOURCE_TEXT_UNSAFE"
    REGULATORY_CLAIM_NOT_SUPPORTED = "REGULATORY_CLAIM_NOT_SUPPORTED"
    # About the sources.
    UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"
    UNKNOWN_PRODUCT = "UNKNOWN_PRODUCT"
    UNKNOWN_DOCUMENT = "UNKNOWN_DOCUMENT"
    SOURCE_EXPIRED = "SOURCE_EXPIRED"
    PRODUCT_NOT_CURRENT = "PRODUCT_NOT_CURRENT"
    TOPIC_PRESENT_IN_SOURCES = "TOPIC_PRESENT_IN_SOURCES"
    # About the answer as a whole.
    NO_APPROVED_FACT = "NO_APPROVED_FACT"
    COMPARISON_NEEDS_TWO_PRODUCTS = "COMPARISON_NEEDS_TWO_PRODUCTS"
    RENDERED_TEXT_FAILED_CHECK = "RENDERED_TEXT_FAILED_CHECK"
    PERSONAL_ADVICE_NOT_OFFERED = "PERSONAL_ADVICE_NOT_OFFERED"
    OUTCOME_DECIDED_BY_THE_BANK = "OUTCOME_DECIDED_BY_THE_BANK"
    ACTION_ONLY_ON_BANK_CHANNEL = "ACTION_ONLY_ON_BANK_CHANNEL"
    PRIVATE_DATA_NOT_AVAILABLE = "PRIVATE_DATA_NOT_AVAILABLE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    # About the question and the model run.
    SENSITIVE_DATA_IN_QUESTION = "SENSITIVE_DATA_IN_QUESTION"
    PROPOSAL_SCHEMA_INVALID = "PROPOSAL_SCHEMA_INVALID"
    UNAUTHORIZED_TOOL_ATTEMPT = "UNAUTHORIZED_TOOL_ATTEMPT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_REFUSED = "MODEL_REFUSED"
    MODEL_OUTPUT_TRUNCATED = "MODEL_OUTPUT_TRUNCATED"
    MODEL_ROUND_LIMIT = "MODEL_ROUND_LIMIT"
    MODEL_BUDGET_EXHAUSTED = "MODEL_BUDGET_EXHAUSTED"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- what the model proposes ----------------------------------------------------------------------

_ShortRef = Annotated[str, StringConstraints(max_length=64)]
_ClaimRef = Annotated[str, StringConstraints(max_length=160)]


class ProposedClaim(_Frozen):
    """One fact the model suggests. Untrusted: every field is compared with the verified fact."""

    claim_id: _ClaimRef
    product_id: _ShortRef | None
    claim_type: ClaimType
    field: _ShortRef
    value: Annotated[str, StringConstraints(max_length=400)] | None
    source_id: _ShortRef | None
    source_version: _ShortRef | None
    source_digest: _ShortRef | None


class ModelProposal(_Frozen):
    """A model's whole proposal. Every field is required, and there is no field for prose."""

    intent: Intent
    product_ids: Annotated[tuple[_ShortRef, ...], Field(max_length=MAX_PRODUCTS)]
    claims: Annotated[tuple[ProposedClaim, ...], Field(max_length=MAX_PROPOSED_CLAIMS)]
    process_step: ProcessStep | None
    not_established_topics: Annotated[tuple[NotEstablishedTopic, ...], Field(max_length=MAX_TOPICS)]
    document_id: _ShortRef | None
    document_version: _ShortRef | None


def proposal_json_schema() -> dict[str, Any]:
    """`ModelProposal` for the API's structured output. It uses only keywords the API supports."""

    def enum(values: type[StrEnum]) -> dict[str, Any]:
        return {"type": "string", "enum": [member.value for member in values]}

    def nullable(schema: dict[str, Any]) -> dict[str, Any]:
        return {"anyOf": [schema, {"type": "null"}]}

    text: dict[str, Any] = {"type": "string"}
    claim_fields: dict[str, Any] = {
        "claim_id": text,
        "product_id": nullable(text),
        "claim_type": enum(ClaimType),
        "field": text,
        "value": nullable(text),
        "source_id": nullable(text),
        "source_version": nullable(text),
        "source_digest": nullable(text),
    }
    fields: dict[str, Any] = {
        "intent": enum(Intent),
        "product_ids": {"type": "array", "items": text},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": claim_fields,
                "required": list(claim_fields),
                "additionalProperties": False,
            },
        },
        "process_step": nullable(enum(ProcessStep)),
        "not_established_topics": {"type": "array", "items": enum(NotEstablishedTopic)},
        "document_id": nullable(text),
        "document_version": nullable(text),
    }
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


# --- what the bank holds --------------------------------------------------------------------------


class FactSource(_Frozen):
    source_id: str
    source_version: str
    source_timestamp: str | None
    integrity_digest: str | None
    support: SupportKind
    evidence_span: str | None = None  # the exact words in the cited document, if it is one


class VerifiedFact(_Frozen):
    """One thing the bank may state, with the source that supports it. Built by the bank only."""

    claim_id: str
    provider_id: str
    product_id: str | None
    product_version: str | None
    claim_type: ClaimType
    field: str
    canonical_value: str
    statement: str
    source: FactSource
    narrative: bool = False  # provider prose: never forwarded into model traffic
    mock: bool = True

    def for_model(self) -> dict[str, Any]:
        """What a proposing model may read. A narrative value stays out of model traffic."""
        return {
            "claim_id": self.claim_id,
            "product_id": self.product_id,
            "claim_type": self.claim_type.value,
            "field": self.field,
            "value": None if self.narrative else self.canonical_value,
            "source_id": self.source.source_id,
            "source_version": self.source.source_version,
            "source_digest": self.source.integrity_digest,
        }


# --- what is released -----------------------------------------------------------------------------


class ValidatedClaim(_Frozen):
    """A released claim: every value here comes from the verified fact, none from the model."""

    claim_id: str
    provider_id: str
    product_id: str | None
    product_version: str | None
    claim_type: ClaimType
    field: str
    canonical_value: str
    statement: str
    source_id: str
    source_version: str
    source_timestamp: str | None
    integrity_digest: str | None
    support: SupportKind
    evidence_span: str | None
    validation_outcome: Literal["VERIFIED"] = "VERIFIED"
    reason_code: None = None
    mock: bool

    @classmethod
    def from_fact(cls, fact: VerifiedFact) -> "ValidatedClaim":
        return cls(
            claim_id=fact.claim_id,
            provider_id=fact.provider_id,
            product_id=fact.product_id,
            product_version=fact.product_version,
            claim_type=fact.claim_type,
            field=fact.field,
            canonical_value=fact.canonical_value,
            statement=fact.statement,
            source_id=fact.source.source_id,
            source_version=fact.source.source_version,
            source_timestamp=fact.source.source_timestamp,
            integrity_digest=fact.source.integrity_digest,
            support=fact.source.support,
            evidence_span=fact.source.evidence_span,
            mock=fact.mock,
        )


class RejectedClaim(_Frozen):
    """A refused claim. Holds the reason only: no value or text the model wrote is ever echoed."""

    claim_type: ClaimType
    known_claim_id: str | None  # only an id that exactly names a verified fact
    validation_outcome: Literal["REJECTED"] = "REJECTED"
    reason_code: ReasonCode


class SourceConsulted(_Frozen):
    source_id: str
    source_version: str
    source_timestamp: str | None
    integrity_digest: str | None


class GeneratedBy(_Frozen):
    proposer: str
    model: str | None = None
    model_prose_released: Literal[False] = False


class RegulatedAnswer(_Frozen):
    """The only shape in which the agent-facing surface answers a question."""

    contract_version: Literal["regulated-answer/1"] = CONTRACT_VERSION
    outcome: AnswerOutcome
    reason_code: ReasonCode | None = None
    statements: tuple[str, ...]
    claims: tuple[ValidatedClaim, ...] = ()
    rejected_claims: tuple[RejectedClaim, ...] = ()
    sources_consulted: tuple[SourceConsulted, ...] = ()
    provider_id: str
    product_ids: tuple[str, ...] = ()
    process_step: ProcessStep | None = None
    policy_version: str
    checked_at: str
    nature: str = NATURE
    generated_by: GeneratedBy
    answer_digest: str

    @property
    def released(self) -> bool:
        return self.outcome not in WITHHELD_OUTCOMES

    def as_artifact(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class QuestionRequest(_Frozen):
    """An agent's question. Bounded and identified, and its text is treated as untrusted data."""

    question: Annotated[str, StringConstraints(min_length=1, max_length=MAX_QUESTION_CHARS)]
    provider_id: OpaqueId
    product_ids: Annotated[tuple[OpaqueId, ...], Field(max_length=MAX_PRODUCTS)] = ()
