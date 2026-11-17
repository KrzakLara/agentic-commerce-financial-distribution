"""Distribution governance: who an onboarding is commercially attributed to, and why. SYNTHETIC.

Deterministic rules over recorded claims. No model takes part, no agent text counts as a claim, and
a decision is append-only: a later claim produces a later decision and overwrites nothing.

Kept apart on purpose: the product provider, the depot operator, the distributor or partner bank,
the advising party, the personal agent's technology provider and the customer. A commercial conflict
does not stop the customer's account. It withholds the commercial result until a person decides, and
it is never resolved in favour of the provider.

The sample contracts are invented. They do not represent real fund-distribution contracts. Whether
any commission is permissible at all is a compliance question (for example WpHG section 70), which
is
why every attributed result carries `inducement_review_required`.
"""

import base64
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.exceptions import NotFoundError
from app.domain.application import DepotApplication
from app.domain.catalog import ChannelKind
from app.domain.enums import AuditActorType, AuditOutcome
from app.integrations.jose_tools import SignatureRejected, sha256_hex, verify_jwt
from app.integrations.ports import ProviderDiscoveryPort
from app.integrations.signed_events import consume_jti
from app.repositories.interfaces import UnitOfWork
from app.repositories.trust_orm import (
    AttributionClaimRow,
    AttributionDecisionRow,
    PartnerRelationshipRow,
)
from app.services.application_service import ApplicationService
from app.services.review_service import OperatorNotPermittedError, resolve_dev_operator

REFERRAL_TYPE = "partner-referral+jwt"
SOURCE_CHANNEL = "REGISTERED_CHANNEL"
SOURCE_REFERRAL = "SIGNED_REFERRAL"
SOURCE_RELATIONSHIP = "VERIFIED_RELATIONSHIP"

OUTCOME_DIRECT = "DIRECT_NO_PARTNER"
OUTCOME_PARTNER = "PARTNER_ATTRIBUTED"
OUTCOME_CONFLICT = "CONFLICT_HUMAN_REVIEW"
OUTCOME_RESOLVED = "RESOLVED_BY_PERSON"

ROLES = {
    "product_provider": "The fund or product provider: states the product's terms.",
    "depot_operator": "The licensed depot provider: the only source of OPENED.",
    "distributor_or_partner_bank": "May be attributed under a contract. Never set by an agent.",
    "advising_party": "Whoever gives investment advice, if anyone. This journey gives none.",
    "agent_technology_provider": "Runs the personal agent. Never receives attribution here.",
    "customer": "Decides. May choose the channel and sees the disclosure.",
}


@dataclass(frozen=True)
class SampleContract:
    partner_id: str
    contract_version: str
    effective_from: date
    effective_until: date | None
    commission_eur_cents: dict[str, int]  # per product; a product not listed is not eligible


@dataclass(frozen=True)
class AttributionPolicy:
    version: str
    effective_from: date
    contracts: tuple[SampleContract, ...]


POLICY = AttributionPolicy(
    version="attribution-2026.11-synthetic",
    effective_from=date(2026, 11, 1),
    contracts=(
        SampleContract(
            partner_id="sampletown-coop-bank",
            contract_version="sample-2026.11",
            effective_from=date(2026, 1, 1),
            effective_until=None,
            commission_eur_cents={"depot-basic": 1250},
        ),
        SampleContract(
            partner_id="riverside-sample-bank",
            contract_version="sample-2026.11",
            effective_from=date(2026, 1, 1),
            effective_until=None,
            commission_eur_cents={},
        ),
        SampleContract(
            partner_id="example-valley-savings-bank",
            contract_version="sample-2025.01",
            effective_from=date(2025, 1, 1),
            effective_until=date(2025, 12, 31),
            commission_eur_cents={"depot-basic": 800},
        ),
    ),
)


def _contract(policy: AttributionPolicy, partner_id: str) -> SampleContract | None:
    return next((c for c in policy.contracts if c.partner_id == partner_id), None)


def _contract_is_live(contract: SampleContract, today: date) -> bool:
    return contract.effective_from <= today and (
        contract.effective_until is None or today <= contract.effective_until
    )


def decide_attribution(
    *, claims: list[Mapping[str, Any]], product_id: str, policy: AttributionPolicy, today: date
) -> dict[str, Any]:
    """A pure function of the recorded claims, so a dispute can be recomputed later."""
    accepted = [c for c in claims if c["status"] == "ACCEPTED"]
    partners = sorted({str(c["partner_id"]) for c in accepted})
    explanation = [
        f"Claim from {c['source']} for {c['partner_id'] or 'no partner'}: "
        f"{c['status'].lower()} ({c['reason']})."
        for c in claims
    ]
    if len(partners) > 1:
        explanation.append(
            "Partners conflict. No partner and not the provider gets the result; a person decides."
        )
        return {
            "outcome": OUTCOME_CONFLICT,
            "partner_id": None,
            "explanation": explanation,
            "commission": {"status": "WITHHELD", "reason": "attribution_conflict"},
        }
    if not partners:
        explanation.append("No valid partner claim: disclosed direct route, no commission.")
        return {
            "outcome": OUTCOME_DIRECT,
            "partner_id": None,
            "explanation": explanation,
            "commission": {"status": "NOT_APPLICABLE", "reason": "no_partner"},
        }
    partner = partners[0]
    explanation.append(f"One valid partner claim: attributed to {partner}.")
    return {
        "outcome": OUTCOME_PARTNER,
        "partner_id": partner,
        "explanation": explanation,
        "commission": commission_for(partner, product_id, policy, today),
    }


def commission_for(
    partner_id: str, product_id: str, policy: AttributionPolicy, today: date
) -> dict[str, Any]:
    """A demonstration calculation from an invented sample contract. Not a payment instruction."""
    contract = _contract(policy, partner_id)
    if contract is None or not _contract_is_live(contract, today):
        return {"status": "NOT_ELIGIBLE", "reason": "no_current_contract"}
    amount = contract.commission_eur_cents.get(product_id)
    if amount is None:
        return {"status": "NOT_ELIGIBLE", "reason": "product_not_in_the_contract"}
    return {
        "status": "CALCULATED_DEMONSTRATION_ONLY",
        "amount_eur_cents": amount,
        "contract_version": contract.contract_version,
        "inducement_review_required": True,
        "payable": False,
    }


class DistributionGovernanceService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        applications: ApplicationService,
        catalog: ProviderDiscoveryPort,
        *,
        referral_jwks: str | None,
        audience: str,
        policy: AttributionPolicy = POLICY,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._uow_factory = uow_factory
        self._apps = applications
        self._catalog = catalog
        self._referral_jwks: Mapping[str, Any] = json.loads(referral_jwks) if referral_jwks else {}
        self._audience = audience
        self._policy = policy
        self._clock = clock
        self._id_factory = id_factory

    # --- claims -----------------------------------------------------------------------------------

    def _record_claim(
        self,
        uow: UnitOfWork,
        application_id: str,
        *,
        partner_id: str | None,
        source: str,
        status: str,
        reason: str,
        proof_digest: str | None = None,
        terms_version: str | None = None,
    ) -> None:
        uow.trust.add(
            AttributionClaimRow(
                claim_id=f"clm-{self._id_factory()}",
                application_id=application_id,
                partner_id=partner_id,
                source=source,
                status=status,
                reason=reason,
                proof_digest=proof_digest,
                terms_version=terms_version,
                received_at=self._clock(),
            )
        )
        uow.trust.flush()

    def _record_registry_claims(self, uow: UnitOfWork, application: DepotApplication) -> None:
        """Claims only the bank's own register can make. Recorded once per application."""
        existing = uow.trust.scalars(
            select(AttributionClaimRow).where(
                AttributionClaimRow.application_id == application.application_id,
                AttributionClaimRow.source.in_((SOURCE_CHANNEL, SOURCE_RELATIONSHIP)),
            )
        ).first()
        if existing is not None:
            return
        channel = next(
            c
            for c in self._catalog.list_channels(application.provider_id)
            if c.channel_id == application.channel_id
        )
        if channel.kind is ChannelKind.PARTNER_BANK and channel.partner_id:
            self._record_claim(
                uow,
                application.application_id,
                partner_id=channel.partner_id,
                source=SOURCE_CHANNEL,
                status="ACCEPTED",
                reason="customer_confirmed_the_registered_channel",
            )
        relationships = uow.trust.scalars(
            select(PartnerRelationshipRow).where(
                PartnerRelationshipRow.customer_ref == application.customer_ref
            )
        ).all()
        for relationship in relationships:
            self._record_claim(
                uow,
                application.application_id,
                partner_id=relationship.partner_id,
                source=SOURCE_RELATIONSHIP,
                status="ACCEPTED",
                reason="verified_existing_relationship",
            )

    def submit_referral(
        self, *, application: DepotApplication, token: str, actor_id: str, cid: str
    ) -> dict[str, Any]:
        """Check a partner's signed referral, record it either way, and decide again."""
        now = self._clock()
        with self._uow_factory() as uow:
            current = uow.applications.get(application.application_id)
            if current is None:
                raise NotFoundError()
            partner_id, status, reason, terms = self._assess_referral(uow, token, current, now)
            self._record_claim(
                uow,
                current.application_id,
                partner_id=partner_id,
                source=SOURCE_REFERRAL,
                status=status,
                reason=reason,
                proof_digest=sha256_hex(token.encode("utf-8")),
                terms_version=terms,
            )
            uow.audit.append(
                self._apps.new_event(
                    event_type="attribution.referral_recorded",
                    correlation_id=cid,
                    actor_type=AuditActorType.AGENT,
                    actor_id=actor_id,
                    outcome=AuditOutcome.SUCCESS if status == "ACCEPTED" else AuditOutcome.DENIED,
                    application_id=current.application_id,
                    details={"partner_id": partner_id, "status": status, "reason": reason},
                )
            )
            decision = self._decide(uow, current, cid, decided_by="policy")
            uow.commit()
            return decision

    def _assess_referral(
        self, uow: UnitOfWork, token: str, application: DepotApplication, now: datetime
    ) -> tuple[str | None, str, str, str | None]:
        """(partner, status, reason, terms version). Any doubt is a rejection, never a guess."""
        partner_id = _unverified_issuer(token)
        jwks = self._referral_jwks.get(partner_id or "")
        if not partner_id or not jwks:
            return None, "REJECTED", "unknown_partner", None
        try:
            _, claims = verify_jwt(token, jwks, typ=REFERRAL_TYPE)
        except SignatureRejected:
            return partner_id, "REJECTED", "forged_referral", None
        terms = claims.get("terms_version")
        expires = claims.get("exp")
        if claims.get("iss") != partner_id or claims.get("aud") != self._audience:
            return partner_id, "REJECTED", "wrong_audience", None
        if type(expires) is not int or now.timestamp() >= expires:
            return partner_id, "REJECTED", "referral_expired", None
        if claims.get("product_id") != application.product_id:
            return partner_id, "REJECTED", "wrong_product", None
        contract = _contract(self._policy, partner_id)
        if contract is None or not _contract_is_live(contract, now.date()):
            return partner_id, "REJECTED", "no_current_contract", None
        if terms != contract.contract_version:
            return partner_id, "REJECTED", "terms_outdated", None
        jti = claims.get("jti")
        if not isinstance(jti, str) or not consume_jti(
            uow, "partner_referral", jti[:128], datetime.fromtimestamp(expires, UTC)
        ):
            return partner_id, "REJECTED", "referral_replayed", None
        return partner_id, "ACCEPTED", "valid_signed_referral", str(terms)

    # --- decisions --------------------------------------------------------------------------------

    def _decide(
        self, uow: UnitOfWork, application: DepotApplication, cid: str, *, decided_by: str
    ) -> dict[str, Any]:
        self._record_registry_claims(uow, application)
        rows = uow.trust.scalars(
            select(AttributionClaimRow)
            .where(AttributionClaimRow.application_id == application.application_id)
            .order_by(AttributionClaimRow.received_at, AttributionClaimRow.claim_id)
        ).all()
        claims = [
            {
                "claim_id": row.claim_id,
                "partner_id": row.partner_id,
                "source": row.source,
                "status": row.status,
                "reason": row.reason,
                "proof_digest": row.proof_digest,
                "terms_version": row.terms_version,
            }
            for row in rows
        ]
        result = decide_attribution(
            claims=claims,
            product_id=application.product_id,
            policy=self._policy,
            today=self._clock().date(),
        )
        return self._store(uow, application.application_id, result, claims, cid, decided_by)

    def _store(
        self,
        uow: UnitOfWork,
        application_id: str,
        result: dict[str, Any],
        claims: list[dict[str, Any]],
        cid: str,
        decided_by: str,
    ) -> dict[str, Any]:
        sequence = (
            uow.trust.scalar(
                select(func.max(AttributionDecisionRow.sequence)).where(
                    AttributionDecisionRow.application_id == application_id
                )
            )
            or 0
        ) + 1
        row = AttributionDecisionRow(
            decision_id=f"atd-{self._id_factory()}",
            application_id=application_id,
            sequence=sequence,
            outcome=result["outcome"],
            partner_id=result["partner_id"],
            policy_version=self._policy.version,
            inputs={"claims": claims},
            explanation=result["explanation"],
            commission=result["commission"],
            decided_by=decided_by,
            decided_at=self._clock(),
        )
        uow.trust.add(row)
        uow.audit.append(
            self._apps.new_event(
                event_type="attribution.decided",
                correlation_id=cid,
                actor_type=AuditActorType.SYSTEM,
                actor_id=None,
                outcome=AuditOutcome.SUCCESS,
                application_id=application_id,
                details={
                    "decision_id": row.decision_id,
                    "sequence": sequence,
                    "outcome": row.outcome,
                    "partner_id": row.partner_id,
                    "policy_version": row.policy_version,
                    "commission_status": row.commission["status"],
                    "decided_by": decided_by,
                },
            )
        )
        return _view(row)

    def decide_in(self, uow: UnitOfWork, application: DepotApplication, cid: str) -> dict[str, Any]:
        """Inside the caller's transaction. Reuses the latest decision while no claim has
        changed."""
        latest = _latest(uow, application.application_id)
        if latest is not None:
            return _view(latest)
        return self._decide(uow, application, cid, decided_by="policy")

    def resolve_by_person(
        self, *, operator_id: str, application_id: str, partner_id: str | None, cid: str
    ) -> dict[str, Any]:
        """A person settles a conflict among the accepted claims. Trusted console only.

        Only a known operator principal may decide: an agent id, a model name or any other
        string is refused, so no attribution can be recorded as decided by a machine.
        """
        if resolve_dev_operator(operator_id) is None:
            raise OperatorNotPermittedError()
        with self._uow_factory() as uow:
            latest = _latest(uow, application_id)
            application = uow.applications.get(application_id)
            if latest is None or application is None or latest.outcome != OUTCOME_CONFLICT:
                raise NotFoundError()
            accepted = {
                str(c["partner_id"]) for c in latest.inputs["claims"] if c["status"] == "ACCEPTED"
            }
            if partner_id is not None and partner_id not in accepted:
                raise NotFoundError()  # a person chooses among claims; nobody invents a partner
            commission = (
                commission_for(
                    partner_id, application.product_id, self._policy, self._clock().date()
                )
                if partner_id
                else {"status": "NOT_APPLICABLE", "reason": "resolved_without_partner"}
            )
            result = {
                "outcome": OUTCOME_RESOLVED,
                "partner_id": partner_id,
                "explanation": [
                    *latest.explanation,
                    f"Resolved by {operator_id}: {partner_id or 'no partner'}.",
                ],
                "commission": commission,
            }
            view = self._store(
                uow, application_id, result, latest.inputs["claims"], cid, operator_id
            )
            uow.commit()
            return view

    def latest_decision(self, application_id: str) -> dict[str, Any] | None:
        with self._uow_factory() as uow:
            row = _latest(uow, application_id)
            return _view(row) if row else None


def _latest(uow: UnitOfWork, application_id: str) -> AttributionDecisionRow | None:
    return uow.trust.scalars(
        select(AttributionDecisionRow)
        .where(AttributionDecisionRow.application_id == application_id)
        .order_by(AttributionDecisionRow.sequence.desc())
    ).first()


def _view(row: AttributionDecisionRow) -> dict[str, Any]:
    return {
        "decision_id": row.decision_id,
        "sequence": row.sequence,
        "outcome": row.outcome,
        "partner_id": row.partner_id,
        "policy_version": row.policy_version,
        "explanation": list(row.explanation),
        "commission": dict(row.commission),
        "decided_by": row.decided_by,
    }


def _unverified_issuer(token: str) -> str | None:
    """Which partner's keys to try. Nothing is believed until that signature checks out."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, IndexError, TypeError):
        return None
    issuer = claims.get("iss") if isinstance(claims, dict) else None
    return issuer[:64] if isinstance(issuer, str) else None
