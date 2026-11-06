"""A versioned, explainable applicability matrix for regulatory duties. An engineering control.

Passing this code or its tests proves nothing about legal compliance. Each rule names the source it
is derived from and distinguishes three things: what is a duty in a clear case, what this prototype
decides as a design assumption, and what stays UNRESOLVED until compliance or legal confirms it.

Opening an account is modelled apart from executing an order and from advice. In particular, opening
this account does not by itself trigger the appropriateness test, and a warning never becomes an
automatic rejection (WpHG section 63(10)). The AI never takes a decision here: a request naming a
model
as the decider is refused.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

POLICY_VERSION = "regulatory-matrix-2026.11-engineering"


class Service(StrEnum):
    ACCOUNT_OPENING = "ACCOUNT_OPENING"
    PRODUCT_INFORMATION = "PRODUCT_INFORMATION"
    ORDER_NON_ADVISED = "ORDER_NON_ADVISED"
    ORDER_EXECUTION_ONLY = "ORDER_EXECUTION_ONLY"
    INVESTMENT_ADVICE = "INVESTMENT_ADVICE"
    PORTFOLIO_MANAGEMENT = "PORTFOLIO_MANAGEMENT"


class Applies(StrEnum):
    REQUIRED = "REQUIRED"
    NOT_TRIGGERED = "NOT_TRIGGERED"
    UNRESOLVED = "UNRESOLVED"  # compliance or legal must confirm: never treated as satisfied
    NOT_OFFERED = "NOT_OFFERED"


class Gate(StrEnum):
    BLOCKING = "BLOCKING"  # a genuine mandatory gate: fails closed
    WARNING = "WARNING"  # warns the customer; the customer decides; no automatic rejection
    HUMAN = "HUMAN"  # goes to a person
    NONE = "NONE"


class Evidence(StrEnum):
    SATISFIED = "SATISFIED"
    SATISFIED_SYNTHETIC = "SATISFIED_SYNTHETIC"  # the mechanism ran; it has no regulatory value
    MISSING = "MISSING"
    NOT_NEEDED = "NOT_NEEDED"


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    source: str
    applies: str
    gate: str
    evidence: str
    reason_code: str
    required_evidence: tuple[str, ...]
    legal_confirmation_required: bool

    @property
    def blocks(self) -> bool:
        return self.gate == Gate.BLOCKING and self.evidence == Evidence.MISSING


def _rule(
    rule_id: str,
    source: str,
    applies: Applies,
    gate: Gate,
    evidence: Evidence,
    reason: str,
    required: tuple[str, ...] = (),
    *,
    confirm: bool = False,
) -> RuleResult:
    return RuleResult(
        rule_id, source, applies.value, gate.value, evidence.value, reason, required, confirm
    )


_ORDERS = (Service.ORDER_NON_ADVISED, Service.ORDER_EXECUTION_ONLY)
_ADVICE = (Service.INVESTMENT_ADVICE, Service.PORTFOLIO_MANAGEMENT)


def _identification(service: Service, facts: Mapping[str, Any]) -> RuleResult:
    source = "GwG sections 10(1) no. 1, 11(1), 12"
    if service is not Service.ACCOUNT_OPENING:
        return _rule(
            "AML-IDENTIFICATION",
            source,
            Applies.NOT_TRIGGERED,
            Gate.NONE,
            Evidence.NOT_NEEDED,
            "business_relationship_not_being_established",
        )
    status = facts.get("identity_proofing")
    if status == "VERIFIED":
        evidence = (
            Evidence.SATISFIED
            if facts.get("regulatory_evidence")
            else (Evidence.SATISFIED_SYNTHETIC)
        )
        reason = "identity_proofing_result_recorded"
    else:
        evidence, reason = Evidence.MISSING, "identity_proofing_result_missing"
    return _rule(
        "AML-IDENTIFICATION",
        source,
        Applies.REQUIRED,
        Gate.BLOCKING,
        evidence,
        reason,
        ("identity_proofing_result",),
    )


def _appropriateness(service: Service, facts: Mapping[str, Any]) -> RuleResult:
    source = "WpHG section 63(10), (11); MiFID II Art. 25(3), (4)"
    if service is Service.ACCOUNT_OPENING:
        recorded = facts.get("assessment_outcome")
        return _rule(
            "APPROPRIATENESS",
            source,
            Applies.NOT_TRIGGERED,
            Gate.NONE,
            Evidence.NOT_NEEDED,
            "account_opening_is_not_an_order"
            if not recorded
            else "recorded_for_later_orders_not_a_gate_on_the_account",
            confirm=True,
        )
    if service is Service.ORDER_NON_ADVISED:
        outcome = facts.get("assessment_outcome")
        return _rule(
            "APPROPRIATENESS",
            source,
            Applies.REQUIRED,
            Gate.WARNING,
            Evidence.SATISFIED if outcome == "ASSESSMENT_OK" else Evidence.MISSING,
            "warn_if_inappropriate_or_information_insufficient",
            ("knowledge_and_experience", "warning_if_needed"),
        )
    if service is Service.ORDER_EXECUTION_ONLY:
        conditions = ("non_complex_instrument", "customer_initiative", "prior_notice", "conflicts")
        met = all(facts.get(condition) is True for condition in conditions)
        return _rule(
            "APPROPRIATENESS",
            source,
            Applies.UNRESOLVED,
            Gate.HUMAN,
            Evidence.MISSING,
            "execution_only_conditions_stated_compliance_must_confirm"
            if met
            else "execution_only_conditions_not_all_stated",
            conditions,
            confirm=True,
        )
    return _rule(
        "APPROPRIATENESS",
        source,
        Applies.NOT_TRIGGERED,
        Gate.NONE,
        Evidence.NOT_NEEDED,
        "not_this_service",
    )


def _suitability(service: Service) -> RuleResult:
    source = "WpHG section 64(3); MiFID II Art. 25(2)"
    if service in _ADVICE:
        return _rule(
            "SUITABILITY",
            source,
            Applies.NOT_OFFERED,
            Gate.BLOCKING,
            Evidence.MISSING,
            "advice_is_not_offered_in_this_prototype",
            ("suitability_assessment",),
        )
    return _rule(
        "SUITABILITY", source, Applies.NOT_TRIGGERED, Gate.NONE, Evidence.NOT_NEEDED, "no_advice"
    )


def _key_information(service: Service, facts: Mapping[str, Any]) -> RuleResult:
    source = "Regulation (EU) No 1286/2014 (PRIIPs) Art. 13"
    if service in _ORDERS and facts.get("instrument_is_priip") is not False:
        return _rule(
            "KEY-INFORMATION",
            source,
            Applies.UNRESOLVED,
            Gate.HUMAN,
            Evidence.MISSING,
            "instrument_classification_needed",
            ("key_information_document",),
            confirm=True,
        )
    return _rule(
        "KEY-INFORMATION",
        source,
        Applies.NOT_TRIGGERED,
        Gate.NONE,
        Evidence.NOT_NEEDED,
        "no_instrument_is_sold_here",
    )


def _signature(service: Service, facts: Mapping[str, Any]) -> RuleResult:
    source = "Regulation (EU) No 910/2014 (eIDAS) Art. 3(12), Art. 25"
    if service is not Service.ACCOUNT_OPENING:
        return _rule(
            "SIGNATURE-LEVEL", source, Applies.NOT_TRIGGERED, Gate.NONE, Evidence.NOT_NEEDED, "n/a"
        )
    signed = facts.get("contract_signature") == "SIGNED"
    not_due_yet = facts.get("stage") == "consent"  # the customer signs after the confirmation
    return _rule(
        "SIGNATURE-LEVEL",
        source,
        Applies.UNRESOLVED,
        Gate.BLOCKING,
        Evidence.NOT_NEEDED
        if not_due_yet
        else (Evidence.SATISFIED_SYNTHETIC if signed else Evidence.MISSING),
        "required_form_per_contract_is_a_legal_decision_test_signature_has_no_legal_effect",
        ("signature_of_the_exact_package",),
        confirm=True,
    )


def _tax(service: Service, facts: Mapping[str, Any]) -> RuleResult:
    if service is not Service.ACCOUNT_OPENING:
        return _rule(
            "TAX-INFORMATION",
            "Design assumption of the case study",
            Applies.NOT_TRIGGERED,
            Gate.NONE,
            Evidence.NOT_NEEDED,
            "n/a",
        )
    return _rule(
        "TAX-INFORMATION",
        "Design assumption of the case study; the exact duties are for tax and legal counsel",
        Applies.UNRESOLVED,
        Gate.BLOCKING,
        Evidence.SATISFIED_SYNTHETIC if facts.get("tax_information") else Evidence.MISSING,
        "prototype_gate_exact_duties_need_confirmation",
        ("tax_information_status",),
        confirm=True,
    )


def _inducements(facts: Mapping[str, Any]) -> RuleResult:
    source = "WpHG section 70; MiFID II Art. 24(9)"
    if facts.get("partner_attributed"):
        return _rule(
            "INDUCEMENTS",
            source,
            Applies.UNRESOLVED,
            Gate.HUMAN,
            Evidence.MISSING,
            "commission_not_payable_before_compliance_review",
            ("quality_enhancement_and_disclosure_assessment",),
            confirm=True,
        )
    return _rule(
        "INDUCEMENTS", source, Applies.NOT_TRIGGERED, Gate.NONE, Evidence.NOT_NEEDED, "no_partner"
    )


def _data_minimization() -> RuleResult:
    return _rule(
        "DATA-MINIMIZATION",
        "GDPR Art. 5(1)(c), (e); GwG section 8(4) for AML retention",
        Applies.REQUIRED,
        Gate.NONE,
        Evidence.SATISFIED,
        "no_tax_id_document_image_or_biometric_data_is_stored_retention_rules_not_modelled",
        confirm=True,
    )


def _human_decider(facts: Mapping[str, Any]) -> RuleResult:
    decider = facts.get("decision_maker", "bank_policy")
    allowed = decider == "bank_policy"
    return _rule(
        "NO-AI-COMPLIANCE-DECISION",
        "Design rule of ADR-001; assess GDPR Art. 22 per use case",
        Applies.REQUIRED,
        Gate.BLOCKING,
        Evidence.SATISFIED if allowed else Evidence.MISSING,
        "deterministic_bank_policy_decides" if allowed else "a_model_may_not_decide",
    )


def evaluate(service: Service, facts: Mapping[str, Any]) -> dict[str, Any]:
    """The applicable rules for one service, deterministically. Same input, same output."""
    rules = (
        _identification(service, facts),
        _appropriateness(service, facts),
        _suitability(service),
        _key_information(service, facts),
        _signature(service, facts),
        _tax(service, facts),
        _inducements(facts),
        _data_minimization(),
        _human_decider(facts),
    )
    return {
        "policy_version": POLICY_VERSION,
        "service": service.value,
        "allowed": not any(rule.blocks for rule in rules),
        "blocking": [rule.rule_id for rule in rules if rule.blocks],
        "human_review": [
            rule.rule_id
            for rule in rules
            if rule.gate == Gate.HUMAN and rule.applies != "NOT_TRIGGERED"
        ],
        "warnings": [rule.rule_id for rule in rules if rule.gate == Gate.WARNING],
        "unresolved": [rule.rule_id for rule in rules if rule.applies == Applies.UNRESOLVED],
        "rules": [asdict(rule) for rule in rules],
        "statement": "An engineering control. It is not legal advice or proof of compliance.",
    }


def applicability_matrix() -> list[dict[str, Any]]:
    """The matrix in its default state, for the documentation and for reviewers."""
    return [evaluate(service, {}) for service in Service]
