"""TRUSTED dev/test operator workflow for human review (Phase 04). Not an API, not for production.

    python -m scripts.dev_operator --operator op-reviewer-1 list
    python -m scripts.dev_operator --operator op-reviewer-1 show --case rvw-...
    python -m scripts.dev_operator --operator op-reviewer-1 request-info --case rvw-... \\
        --category SOURCE_OF_FUNDS_CONFIRMATION
    python -m scripts.dev_operator --operator op-reviewer-1 add-evidence --case rvw-... \\
        --category SOURCE_OF_FUNDS_CONFIRMATION --reference SYN-SOF-0001
    python -m scripts.dev_operator --operator op-reviewer-1 decide --case rvw-... \\
        --decision RE_EVALUATE
    python -m scripts.dev_operator --operator op-supervisor-1 decide --case rvw-... \\
        --decision REJECT

The bank-staff side of a review case. It works directly on the configured database
(``TRUSTEQ_DATABASE_URL``) through the normal services; there is deliberately NO operator HTTP
endpoint, so no agent, customer or remote caller can reach these actions.

Operators are synthetic dev identities (`app.services.review_service.DEV_OPERATORS`):
``op-reviewer-1`` (REVIEWER) and ``op-supervisor-1`` (SUPERVISOR, may also REJECT). An agent
id, an agent token or a customer session is never an operator credential.

Decisions are a fixed list - RE_EVALUATE, RESUME, REJECT, ACKNOWLEDGE - and none of them
approves a check or opens an account: OPENED only ever comes from the depot provider.

Refuses to run unless ``Settings.dev_operator_cli_active`` (never true in production).
"""

import argparse
import sys
import uuid
from collections.abc import Sequence

from app.container import AppContainer, build_default_container
from app.core.config import Settings
from app.core.exceptions import AppError
from app.domain.enums import ReviewDecision, ReviewEvidenceCategory
from app.repositories.database import create_db_engine, create_session_factory, init_database
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.application_service import ApplicationService
from app.services.authorization_service import AuthorizationService
from app.services.onboarding_service import OnboardingService
from app.services.review_service import CaseDetails, ReviewService, resolve_dev_operator

BANNER = "DEV OPERATOR CONSOLE - synthetic review cases only; no decision can open an account."


def _print_details(details: CaseDetails) -> None:
    case, application = details.case, details.application
    print(f"case {case.case_id}  status={case.status.value}  reason={case.reason.value}")
    if case.request_category:
        print(f"  customer request category: {case.request_category.value}")
    if case.check_type:
        print(f"  check: {case.check_type.value} attempt {case.check_attempt}")
    step = case.interrupted_step.value if case.interrupted_step else "-"
    print(f"  interrupted step: {step}  (pauses workflow: {case.interrupted_step is not None})")
    if case.requested_information:
        print(f"  requested information: {case.requested_information.value}")
    for item in case.evidence:
        print(f"  evidence: {item.category.value} {item.reference} by {item.recorded_by}")
    if case.resolution:
        print(f"  resolution: {case.resolution.value} by {case.resolved_by}")
    print(
        f"application {application.application_id}  state={application.status.value}  "
        f"resume_status={application.resume_status.value if application.resume_status else '-'}"
    )
    print(
        f"  provider={application.provider_id} product={application.product_id} "
        f"channel={application.channel_id}  (MOCK catalog)"
    )
    for check in details.checks:
        print(
            f"  check {check.check_type.value} attempt {check.attempt}: {check.outcome.value} "
            f"({check.reference}, mock={check.mock})"
        )
    print(
        f"  consent recorded: {details.consent_recorded}  "
        f"disclosure recorded: {details.disclosure_recorded}"
    )
    if details.attribution:
        attribution = details.attribution
        print(
            f"  attribution: channel={attribution.channel_id} kind={attribution.channel_kind.value}"
            f" partner={attribution.partner_id or '-'}"
        )
    allowed = ", ".join(sorted(d.value for d in details.allowed_decisions)) or "none (closed)"
    print(f"  allowed decisions: {allowed}")
    print("  audit trail:")
    for event in details.audit:
        print(
            f"    {event.occurred_at.isoformat(timespec='seconds')} {event.event_type} "
            f"{event.actor_type.value}:{event.actor_id or '-'} {event.outcome.value}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--operator", required=True, help="synthetic operator id")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list active (not closed) review cases")
    show = commands.add_parser("show", help="inspect one case with its safe synthetic context")
    show.add_argument("--case", required=True)
    info = commands.add_parser("request-info", help="ask for one permitted kind of evidence")
    info.add_argument("--case", required=True)
    info.add_argument(
        "--category", required=True, choices=[c.value for c in ReviewEvidenceCategory]
    )
    evidence = commands.add_parser("add-evidence", help="record a synthetic evidence reference")
    evidence.add_argument("--case", required=True)
    evidence.add_argument(
        "--category", required=True, choices=[c.value for c in ReviewEvidenceCategory]
    )
    evidence.add_argument("--reference", required=True, help="synthetic id, e.g. SYN-SOF-0001")
    decide = commands.add_parser("decide", help="close the case with one allowed decision")
    decide.add_argument("--case", required=True)
    decide.add_argument("--decision", required=True, choices=[d.value for d in ReviewDecision])
    return parser


def main(
    argv: Sequence[str] | None = None,
    settings: Settings | None = None,
    container: AppContainer | None = None,
) -> int:
    args = _parser().parse_args(argv)
    settings = settings or Settings()
    if not settings.dev_operator_cli_active:
        print(
            "refused: the dev operator console is disabled (or this is production)", file=sys.stderr
        )
        return 2
    operator = resolve_dev_operator(args.operator)
    if operator is None:  # never echo the value: it might be a pasted token
        print(
            "refused: unknown operator identity (agent ids, agent tokens and customer sessions "
            "are never operator credentials)",
            file=sys.stderr,
        )
        return 2
    engine = create_db_engine(settings.database_url)
    try:
        init_database(engine)
        factory = create_session_factory(engine)

        def uow() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(factory)

        container = container or build_default_container(settings)
        applications = ApplicationService(
            uow,
            clock=container.clock,
            catalog=container.catalog,
            authorization=AuthorizationService(uow, clock=container.clock),  # verify-only
        )
        onboarding = OnboardingService(
            uow,
            applications,
            catalog=container.catalog,
            kyc=container.kyc,
            compliance=container.compliance,
            contract=container.contract,
            depot=container.depot,
            clock=container.clock,
            handoff_ttl_seconds=settings.handoff_ttl_seconds,
        )
        reviews = ReviewService(
            uow, applications, advance_checks=onboarding.advance_checks, clock=container.clock
        )
        cid = f"op-cli-{uuid.uuid4().hex[:12]}"
        print(BANNER)
        print(f"operator {operator.operator_id} ({operator.role.value})")
        if args.command == "list":
            cases = reviews.list_active(operator)
            if not cases:
                print("no active review cases")
            for case in cases:
                step = case.interrupted_step.value if case.interrupted_step else "-"
                print(
                    f"{case.case_id}  {case.application_id}  {case.reason.value}  "
                    f"{case.status.value}  step={step}  "
                    f"opened={case.created_at.isoformat(timespec='seconds')}"
                )
        elif args.command == "show":
            _print_details(reviews.inspect(operator, args.case))
        elif args.command == "request-info":
            case = reviews.request_information(
                operator, args.case, ReviewEvidenceCategory(args.category), cid
            )
            print(f"{case.case_id}: {case.status.value}, waiting for {args.category}")
        elif args.command == "add-evidence":
            case = reviews.record_evidence(
                operator, args.case, ReviewEvidenceCategory(args.category), args.reference, cid
            )
            print(f"{case.case_id}: {case.status.value}, {len(case.evidence)} evidence item(s)")
        else:
            application = reviews.decide(operator, args.case, ReviewDecision(args.decision), cid)
            print(
                f"{args.case}: CLOSED with {args.decision}; application "
                f"{application.application_id} is now {application.status.value}"
            )
    except (AppError, ValueError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
