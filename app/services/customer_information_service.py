"""The customer's tax information and knowledge-and-experience answers, in the onboarding journey.

A subflow bound to a live handoff: a request only counts with the one-time link's challenge, from
the application's own customer, while the application still awaits the confirmation. The state
machine is not touched: there is no new state and no transition here. The policy gate
(`evaluate_customer_information`) reads the stored result when the customer confirms and again when
the agent submits.

Everything here reaches the bank through the customer channel (session + CSRF, see
`app.api.routes.customer`). The agent has no tool and no route for it, so these values never enter
model traffic. No model takes part in any decision. Validation is LOCAL: nothing is sent to a tax
authority, and the synthetic tax identifier is discarded after the format check.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.exceptions import InvalidStateError
from app.domain.application import DepotApplication
from app.domain.customer_information import AppropriatenessRecord, TaxInformationRecord
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import AuditActorType, AuditOutcome
from app.repositories.interfaces import UnitOfWork
from app.services.application_service import ApplicationService
from app.services.appropriateness import (
    Answers,
    AnswerSource,
    ExperienceBand,
    ServiceContext,
    assess,
    knowledge_level_for,
)
from app.services.onboarding_service import OnboardingService
from app.services.policy import evaluate_customer_information
from app.services.tax_information import (
    TaxDataSource,
    TaxValidationResult,
    validate_tax_information,
)


@dataclass(frozen=True)
class InformationStatus:
    tax: TaxInformationRecord | None
    assessment: AppropriatenessRecord | None
    ready: bool


class CustomerInformationService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        applications: ApplicationService,
        onboarding: OnboardingService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._apps = applications
        self._onboarding = onboarding
        self._clock = clock

    def status(self, application: DepotApplication) -> InformationStatus:
        """For a page or an answer the caller is already authorized to see."""
        with self._uow_factory() as uow:
            return self._status(uow, application)

    @staticmethod
    def _status(uow: UnitOfWork, application: DepotApplication) -> InformationStatus:
        tax = uow.customer_information.get_tax(application.application_id)
        assessment = uow.customer_information.get_assessment(application.application_id)
        decision = evaluate_customer_information(
            application=application, tax=tax, assessment=assessment
        )
        return InformationStatus(tax=tax, assessment=assessment, ready=decision.allowed)

    def _open_application(
        self,
        uow: UnitOfWork,
        customer_id: str,
        handoff_id: str,
        nonce: str,
        correlation_id: str,
    ) -> DepotApplication:
        """The live handoff's application, re-read in this transaction. Refuses a paused one."""
        review = self._onboarding.review_handoff(
            customer_id=customer_id,
            handoff_id=handoff_id,
            nonce=nonce,
            correlation_id=correlation_id,
        )
        application = uow.applications.get(review.application.application_id)
        if application is None or application.status is not S.AWAITING_AUTHORIZATION:
            self._audit(
                uow,
                "customer_information.rejected",
                customer_id,
                review.application.application_id,
                correlation_id,
                {"reason": "state_not_awaiting_authorization"},
                AuditOutcome.DENIED,
            )
            uow.commit()
            raise InvalidStateError()
        return application

    def _audit(
        self,
        uow: UnitOfWork,
        event_type: str,
        customer_id: str,
        application_id: str,
        correlation_id: str,
        details: Mapping[str, Any],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
    ) -> None:
        uow.audit.append(
            self._apps.new_event(
                event_type=event_type,
                correlation_id=correlation_id,
                actor_type=AuditActorType.CUSTOMER,
                actor_id=customer_id,
                outcome=outcome,
                application_id=application_id,
                details=dict(details),
            )
        )

    def submit_tax(
        self,
        *,
        customer_id: str,
        handoff_id: str,
        nonce: str,
        submitted: Mapping[str, object],
        correlation_id: str,
    ) -> TaxValidationResult:
        """Validate and, if complete, store. An invalid attempt never replaces a stored record."""
        result = validate_tax_information(submitted, TaxDataSource.CUSTOMER_ON_BANK_CHANNEL)
        with self._uow_factory() as uow:
            application = self._open_application(
                uow, customer_id, handoff_id, nonce, correlation_id
            )
            if result.record is not None:
                uow.customer_information.save_tax(
                    TaxInformationRecord(
                        application_id=application.application_id,
                        customer_id=customer_id,
                        source=result.source.value,
                        tax_residency_country=result.record.tax_residency_country,
                        us_person_declared=result.record.us_person_declared,
                        exemption_order_amount_eur=result.record.exemption_order_amount_eur,
                        church_tax_objection=result.record.church_tax_objection,
                        field_statuses={k: v.value for k, v in result.statuses.items()},
                        recorded_at=self._clock(),
                    )
                )
            self._audit(
                uow,
                "customer_information.tax_validated",
                customer_id,
                application.application_id,
                correlation_id,
                result.audit_details(),
                AuditOutcome.SUCCESS if result.complete else AuditOutcome.DENIED,
            )
            uow.commit()
        return result

    def submit_assessment(
        self,
        *,
        customer_id: str,
        handoff_id: str,
        nonce: str,
        knows_capital_loss: bool | None,
        knows_no_guarantee: bool | None,
        experience: ExperienceBand | None,
        correlation_id: str,
    ) -> AppropriatenessRecord:
        """The deterministic assessment of these answers. Asked for the ORDER context on purpose:

        account opening itself does not trigger it (see `app.services.appropriateness`). The bank
        records the result for later orders, and the customer sees it before deciding on the
        account. No outcome stops the account opening.
        """
        with self._uow_factory() as uow:
            application = self._open_application(
                uow, customer_id, handoff_id, nonce, correlation_id
            )
            level = knowledge_level_for(application.product_id)
            result = assess(
                ServiceContext.ORDER_EXECUTION,
                level,
                Answers(
                    source=AnswerSource.CUSTOMER_ON_BANK_CHANNEL,
                    knows_capital_loss=knows_capital_loss,
                    knows_no_guarantee=knows_no_guarantee,
                    experience=experience,
                ),
            )
            record = AppropriatenessRecord(
                application_id=application.application_id,
                customer_id=customer_id,
                product_level=level.value,
                outcome=result.outcome.value,
                reasons=result.reasons,
                knows_capital_loss=knows_capital_loss,
                knows_no_guarantee=knows_no_guarantee,
                experience=experience.value if experience else None,
                rule_set_version=result.rule_set_version,
                recorded_at=self._clock(),
            )
            uow.customer_information.save_assessment(record)
            self._audit(
                uow,
                "customer_information.assessment_recorded",
                customer_id,
                application.application_id,
                correlation_id,
                {
                    "outcome": record.outcome,
                    "reasons": list(record.reasons),
                    "product_level": record.product_level,
                    "rule_set_version": record.rule_set_version,
                },
            )
            uow.commit()
            return record

    def acknowledge_assessment(
        self, *, customer_id: str, handoff_id: str, nonce: str, correlation_id: str
    ) -> AppropriatenessRecord:
        """The customer read the warning or the missing-answers notice and decides to continue."""
        with self._uow_factory() as uow:
            application = self._open_application(
                uow, customer_id, handoff_id, nonce, correlation_id
            )
            record = uow.customer_information.get_assessment(application.application_id)
            if record is None:
                raise InvalidStateError()
            acknowledged = record.model_copy(update={"acknowledged_at": self._clock()})
            uow.customer_information.save_assessment(acknowledged)
            self._audit(
                uow,
                "customer_information.assessment_acknowledged",
                customer_id,
                application.application_id,
                correlation_id,
                {"outcome": record.outcome, "rule_set_version": record.rule_set_version},
            )
            uow.commit()
            return acknowledged
