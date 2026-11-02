"""Application workflow service (trust zone T2).

All business rules for creating applications, reading them on behalf of an actor and moving
them through the state machine live here. Every operation is one unit of work, so state
changes, idempotency records and audit events commit together or not at all.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import (
    ConcurrentModificationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    TransitionNotAuthorizedError,
    UnverifiedReferenceError,
)
from app.domain.application import DepotApplication
from app.domain.audit import AuditEvent
from app.domain.base import DomainModel, OpaqueId
from app.domain.enums import ApplicationStatus, AuditActorType, AuditOutcome, MandateScope
from app.domain.idempotency import IdempotencyRecord
from app.domain.mandate import Mandate
from app.domain.workflow import (
    TERMINAL_STATES,
    TransitionAuthority,
    check_transition,
    next_resume_status,
)
from app.integrations.catalog_fixture import FixtureProviderDiscovery
from app.integrations.ports import ProviderDiscoveryPort
from app.repositories.interfaces import UnitOfWork

OPERATION_CREATE_APPLICATION = "create_application"
MANDATE_AUDIENCE = "trusteq-onboarding-api"
# Scopes of the per-application onboarding mandate (names mirror the agent tool set, ADR-09).
ONBOARDING_SCOPES = frozenset(
    {
        MandateScope.CREATE_DRAFT,
        MandateScope.REQUEST_HANDOFF,
        MandateScope.READ_STATUS,
        MandateScope.SUBMIT_AFTER_CONSENT,
    }
)
_REASON_CODE = re.compile(r"^[a-z0-9_.]{1,64}$")

_AUTHORITY_TO_ACTOR_TYPE = {
    TransitionAuthority.AGENT: AuditActorType.AGENT,
    TransitionAuthority.SYSTEM_POLICY: AuditActorType.SYSTEM,
    TransitionAuthority.CUSTOMER_CHANNEL: AuditActorType.CUSTOMER,
    TransitionAuthority.OPERATOR: AuditActorType.OPERATOR,
    TransitionAuthority.DEPOT_PROVIDER_CONFIRMATION: AuditActorType.PROVIDER,
}


class CreateApplicationCommand(DomainModel):
    """The agent's proposal for a new draft. Untrusted input, validated and immutable."""

    provider_id: OpaqueId
    product_id: OpaqueId
    channel_id: OpaqueId
    customer_ref: OpaqueId

    def request_hash(self) -> str:
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CreateApplicationResult:
    application: DepotApplication
    replayed: bool  # True if an earlier request with the same key/body created it


class ApplicationService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        catalog: ProviderDiscoveryPort | None = None,
        mandate_ttl_seconds: int = 3600,
    ) -> None:
        self._uow_factory = uow_factory
        self._catalog: ProviderDiscoveryPort = catalog or FixtureProviderDiscovery()
        self._mandate_ttl = timedelta(seconds=mandate_ttl_seconds)
        self._clock = clock
        self._id_factory = id_factory

    # --- helpers ------------------------------------------------------------

    def new_event(
        self,
        *,
        event_type: str,
        correlation_id: str,
        actor_type: AuditActorType,
        actor_id: str | None,
        outcome: AuditOutcome,
        application_id: str | None,
        details: dict[str, Any],
    ) -> AuditEvent:
        # `details` must stay free of secrets, idempotency keys, customer data and compliance info.
        return AuditEvent(
            event_id=f"evt-{self._id_factory()}",
            occurred_at=self._clock(),
            correlation_id=correlation_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            application_id=application_id,
            outcome=outcome,
            details=details,
        )

    def _require_verified_references(self, command: CreateApplicationCommand) -> None:
        """Only providers, products and channels of the verified catalog may enter the workflow."""
        try:
            self._catalog.get_provider(command.provider_id)
            products = {p.product_id for p in self._catalog.list_products(command.provider_id)}
            channels = {c.channel_id for c in self._catalog.list_channels(command.provider_id)}
        except NotFoundError:
            raise UnverifiedReferenceError() from None
        if command.product_id not in products or command.channel_id not in channels:
            raise UnverifiedReferenceError()

    # --- create (idempotent) ------------------------------------------------------------

    def create_application(
        self,
        *,
        actor_id: str,
        command: CreateApplicationCommand,
        idempotency_key: str,
        correlation_id: str,
    ) -> CreateApplicationResult:
        self._require_verified_references(command)
        request_hash = command.request_hash()
        now = self._clock()
        application = DepotApplication(
            application_id=f"app-{self._id_factory()}",
            provider_id=command.provider_id,
            product_id=command.product_id,
            channel_id=command.channel_id,
            customer_ref=command.customer_ref,
            agent_actor_id=actor_id,
            created_at=now,
            updated_at=now,
        )
        with self._uow_factory() as uow:
            if uow.idempotency.get(actor_id, OPERATION_CREATE_APPLICATION, idempotency_key) is None:
                try:
                    uow.applications.add(application)
                    mandate = Mandate(
                        mandate_id=f"mdt-{self._id_factory()}",
                        jti=f"jti-{self._id_factory()}",
                        application_id=application.application_id,
                        agent_actor_id=actor_id,
                        customer_id=command.customer_ref,
                        audience=MANDATE_AUDIENCE,
                        scopes=ONBOARDING_SCOPES,
                        issued_at=now,
                        expires_at=now + self._mandate_ttl,
                    )
                    uow.mandates.add(mandate)
                    uow.idempotency.add(
                        IdempotencyRecord(
                            actor_id=actor_id,
                            operation=OPERATION_CREATE_APPLICATION,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                            application_id=application.application_id,
                            created_at=now,
                        )
                    )
                    uow.audit.append(
                        self.new_event(
                            event_type="application.created",
                            correlation_id=correlation_id,
                            actor_type=AuditActorType.AGENT,
                            actor_id=actor_id,
                            outcome=AuditOutcome.SUCCESS,
                            application_id=application.application_id,
                            details={
                                "provider_id": command.provider_id,
                                "product_id": command.product_id,
                                "channel_id": command.channel_id,
                                "status": ApplicationStatus.DRAFT.value,
                                "mandate_id": mandate.mandate_id,
                            },
                        )
                    )
                    uow.commit()
                    return CreateApplicationResult(application, replayed=False)
                except IntegrityError:
                    # A concurrent request with the same (actor, operation, key) won the race.
                    # The UNIQUE constraint rolled our whole attempt back; fall through to replay.
                    uow.rollback()
        return self._replay_or_conflict(
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            correlation_id=correlation_id,
        )

    def _replay_or_conflict(
        self, *, actor_id: str, idempotency_key: str, request_hash: str, correlation_id: str
    ) -> CreateApplicationResult:
        with self._uow_factory() as uow:
            record = uow.idempotency.get(actor_id, OPERATION_CREATE_APPLICATION, idempotency_key)
            if record is None:  # pragma: no cover - the insert failed for an unrelated reason
                raise RuntimeError("application creation failed without an idempotency record")
            existing = uow.applications.get(record.application_id)
            if existing is None:  # pragma: no cover - FK guarantees the application exists
                raise RuntimeError("idempotency record refers to a missing application")
            if record.request_hash == request_hash:
                return CreateApplicationResult(existing, replayed=True)
            uow.audit.append(
                self.new_event(
                    event_type="application.idempotency_conflict",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.AGENT,
                    actor_id=actor_id,
                    outcome=AuditOutcome.DENIED,
                    application_id=existing.application_id,
                    details={"operation": OPERATION_CREATE_APPLICATION},
                )
            )
            uow.commit()
        raise IdempotencyConflictError()

    # --- read (actor-scoped) ------------------------------------------------------------

    def get_application(
        self, *, actor_id: str, application_id: str, correlation_id: str
    ) -> DepotApplication:
        """Return the application only to the actor that owns it.

        Another actor's application is reported exactly like a missing one (404), so ids
        cannot be probed; the attempt is audited.
        """
        with self._uow_factory() as uow:
            return self.load_owned(uow, actor_id, application_id, correlation_id)

    def load_owned(
        self, uow: UnitOfWork, actor_id: str, application_id: str, correlation_id: str
    ) -> DepotApplication:
        """Load an application inside the caller's unit of work, only for its owner.

        A foreign application is indistinguishable from a missing one (404); the attempt is
        audited in its own committed transaction before the error is raised.
        """
        application = uow.applications.get(application_id)
        if application is None:
            raise NotFoundError()
        if application.agent_actor_id != actor_id:
            uow.rollback()
            uow.audit.append(
                self.new_event(
                    event_type="application.access_denied",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.AGENT,
                    actor_id=actor_id,
                    outcome=AuditOutcome.DENIED,
                    application_id=application_id,
                    details={"reason": "not_owner"},
                )
            )
            uow.commit()
            raise NotFoundError()
        return application

    # --- state machine ------------------------------------------------------------

    def transition(
        self,
        application_id: str,
        target: ApplicationStatus,
        *,
        authority: TransitionAuthority,
        reason_code: str,
        correlation_id: str,
        actor_id: str | None = None,
    ) -> DepotApplication:
        """Move an application along an allowed edge, atomically.

        Internal use only (trusted callers). No API route exposes this, and callers choose
        the `authority` in code - it is never read from a request.
        The status update, the audit event and the version bump commit together; a rejected
        attempt is audited in its own transaction and then raised.
        """
        if not _REASON_CODE.fullmatch(reason_code):
            raise ValueError("reason_code must be a short machine code like 'kyc_passed'")

        actor_type = _AUTHORITY_TO_ACTOR_TYPE[authority]
        with self._uow_factory() as uow:
            current = uow.applications.get(application_id)
            if current is None:
                raise NotFoundError()
            try:
                check_transition(current.status, target, authority, current.resume_status)
            except (InvalidTransitionError, TransitionNotAuthorizedError) as exc:
                uow.rollback()
                uow.audit.append(
                    self.new_event(
                        event_type="application.transition_rejected",
                        correlation_id=correlation_id,
                        actor_type=actor_type,
                        actor_id=actor_id,
                        outcome=AuditOutcome.DENIED,
                        application_id=application_id,
                        details={
                            "from": current.status.value,
                            "to": target.value,
                            "authority": authority.value,
                            "reason": exc.code,
                        },
                    )
                )
                uow.commit()
                raise

            result = self.apply_transition(
                uow,
                current,
                target,
                authority=authority,
                reason_code=reason_code,
                correlation_id=correlation_id,
                actor_id=actor_id,
            )
            uow.commit()
            return result

    def apply_transition(
        self,
        uow: UnitOfWork,
        current: DepotApplication,
        target: ApplicationStatus,
        *,
        authority: TransitionAuthority,
        reason_code: str,
        correlation_id: str,
        actor_id: str | None = None,
    ) -> DepotApplication:
        """Take one edge INSIDE the caller's unit of work (no commit).

        Used by multi-step trusted flows (handoff, confirmation, submission, provider
        confirmation) so that the state change, its audit event, the mandate revocation on
        terminal states and the flow's own writes commit together. The edge is checked against
        the state machine here; the update is a compare-and-set on (version, status).
        """
        if not _REASON_CODE.fullmatch(reason_code):
            raise ValueError("reason_code must be a short machine code like 'kyc_passed'")
        check_transition(current.status, target, authority, current.resume_status)
        now = self._clock()
        updated = uow.applications.update_status(
            current.application_id,
            expected_version=current.version,
            from_status=current.status,
            to_status=target,
            resume_status=next_resume_status(current.status, target),
            updated_at=now,
        )
        if not updated:
            uow.rollback()
            raise ConcurrentModificationError()
        details: dict[str, Any] = {
            "from": current.status.value,
            "to": target.value,
            "authority": authority.value,
            "reason_code": reason_code,
            "version": current.version + 1,
        }
        if target in TERMINAL_STATES and uow.mandates.revoke(current.application_id, now):
            details["mandate_revoked"] = True  # the onboarding mandate ends with the workflow
        uow.audit.append(
            self.new_event(
                event_type="application.transition",
                correlation_id=correlation_id,
                actor_type=_AUTHORITY_TO_ACTOR_TYPE[authority],
                actor_id=actor_id,
                outcome=AuditOutcome.SUCCESS,
                application_id=current.application_id,
                details=details,
            )
        )
        result = uow.applications.get(current.application_id)
        if result is None:  # pragma: no cover - rows are never deleted
            raise RuntimeError("application vanished during a transition")
        return result

    # --- internal read model for operators/tests (never returned to the agent) ---------------

    def audit_trail(self, application_id: str) -> list[AuditEvent]:
        with self._uow_factory() as uow:
            return uow.audit.list_for_application(application_id)
