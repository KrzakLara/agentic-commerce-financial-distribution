"""ApplicationService: creation, idempotency, ownership, transitions, audit, atomicity, races."""

import json
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Engine, text

from app.core.exceptions import (
    ConcurrentModificationError,
    ConflictError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    TransitionNotAuthorizedError,
)
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import AuditActorType, AuditOutcome
from app.domain.mandate import AGENT_API_AUDIENCE
from app.domain.workflow import TransitionAuthority as A
from app.repositories.interfaces import UnitOfWork
from app.repositories.sqlalchemy_repositories import SqlAlchemyAuditRepository
from app.services.application_service import (
    ApplicationService,
    CreateApplicationCommand,
    CreateApplicationResult,
)
from app.services.authorization_service import AgentPrincipal

CMD = CreateApplicationCommand(
    provider_id="fixture-bank",
    product_id="depot-basic",
    channel_id="direct",
    customer_ref="opaque-id",
)
OTHER_CMD = CreateApplicationCommand(
    provider_id="fixture-bank",
    product_id="depot-basic",
    channel_id="partner-1",
    customer_ref="opaque-id",
)
CORR = "corr-1"

HAPPY_PATH = [
    (S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY),
    (S.AUTHORIZED, A.CUSTOMER_CHANNEL),
    (S.KYC_PENDING, A.SYSTEM_POLICY),
    (S.COMPLIANCE_PENDING, A.SYSTEM_POLICY),
    (S.CONTRACT_PENDING, A.SYSTEM_POLICY),
    (S.SUBMITTED_TO_DEPOT_PROVIDER, A.SYSTEM_POLICY),
    (S.OPENED, A.DEPOT_PROVIDER_CONFIRMATION),
]


def new_key() -> str:
    return str(uuid.uuid4())


def bootstrap_principal(
    service: ApplicationService, actor: str = "agent-a", customer: str = "opaque-id"
) -> AgentPrincipal:
    """A verified principal for a FRESH bootstrap grant (trusted test bootstrap)."""
    authz = service.authorization
    token = authz.issue_bootstrap(agent_actor_id=actor, customer_id=customer, correlation_id=CORR)
    return authz.authenticate(token.token, audience=AGENT_API_AUDIENCE, correlation_id=CORR)


def status_principal(
    service: ApplicationService, result: CreateApplicationResult
) -> AgentPrincipal:
    """The read-only status credential the bank issued with this application."""
    assert result.credentials is not None
    return service.authorization.authenticate(
        result.credentials.status.token, audience=AGENT_API_AUDIENCE, correlation_id=CORR
    )


def create(service: ApplicationService, actor: str = "agent-a", cmd=CMD, key: str | None = None):  # noqa: ANN001, ANN201
    return service.create_application(
        principal=bootstrap_principal(service, actor, cmd.customer_ref),
        command=cmd,
        idempotency_key=key or new_key(),
        correlation_id=CORR,
    )


def move(service: ApplicationService, app_id: str, target: S, authority: A, reason: str = "test"):  # noqa: ANN201
    return service.transition(
        app_id, target, authority=authority, reason_code=reason, correlation_id=CORR
    )


def count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608


def application_events(engine: Engine) -> int:
    """Audit events about applications. (Phase 03: each test bootstrap grant is audited too,
    as an operator event without an application; those are not business events.)"""
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT COUNT(*) FROM audit_events WHERE application_id IS NOT NULL")
        ).scalar_one()


# --- creation ------------------------------------------------------------


def test_create_makes_a_persisted_draft_owned_by_the_actor(service: ApplicationService) -> None:
    result = create(service)
    app = result.application
    assert result.replayed is False
    assert app.status is S.DRAFT and app.version == 1 and app.resume_status is None
    assert app.agent_actor_id == "agent-a"
    assert app.application_id.startswith("app-") and len(app.application_id) > 20  # unguessable
    assert app.created_at == app.updated_at and app.created_at.tzinfo is not None


# --- idempotency ------------------------------------------------------------


def test_same_actor_key_and_body_returns_the_original_application(
    service: ApplicationService, engine: Engine
) -> None:
    key = new_key()
    first = create(service, key=key)
    second = create(service, key=key)
    assert second.replayed is True
    assert second.application == first.application
    assert count(engine, "applications") == 1
    assert count(engine, "idempotency_records") == 1
    assert application_events(engine) == 1  # a replay is not a new business event


def test_same_key_with_a_different_body_conflicts_and_is_audited(
    service: ApplicationService, engine: Engine
) -> None:
    key = new_key()
    first = create(service, key=key).application
    with pytest.raises(IdempotencyConflictError) as exc_info:
        create(service, cmd=OTHER_CMD, key=key)
    assert exc_info.value.status_code == 409
    assert count(engine, "applications") == 1  # nothing was created
    events = service.audit_trail(first.application_id)
    assert [e.event_type for e in events] == [
        "application.created",
        "application.idempotency_conflict",
    ]
    assert events[1].outcome is AuditOutcome.DENIED
    assert key not in json.dumps([e.model_dump(mode="json") for e in events])  # key never logged


def test_different_actors_never_share_an_idempotency_key(service: ApplicationService) -> None:
    key = new_key()
    a = create(service, actor="agent-a", key=key).application
    b = create(service, actor="agent-b", key=key)
    assert b.replayed is False
    assert b.application.application_id != a.application_id
    assert (a.agent_actor_id, b.application.agent_actor_id) == ("agent-a", "agent-b")


def test_different_keys_create_different_applications(service: ApplicationService) -> None:
    assert create(service).application.application_id != create(service).application.application_id


def test_request_hash_is_stable_and_body_sensitive() -> None:
    assert CMD.request_hash() == CMD.model_copy().request_hash()
    assert CMD.request_hash() != OTHER_CMD.request_hash()


# --- actor-scoped reads ------------------------------------------------------------


def test_owner_can_read_and_other_actor_gets_not_found_and_an_audit_entry(
    service: ApplicationService,
) -> None:
    created = create(service, actor="agent-a")
    app = created.application
    owner = status_principal(service, created)
    assert (
        service.get_application(
            principal=owner, application_id=app.application_id, correlation_id=CORR
        )
        == app
    )
    intruder = status_principal(service, create(service, actor="agent-b"))  # valid, own app
    with pytest.raises(NotFoundError):
        service.get_application(
            principal=intruder, application_id=app.application_id, correlation_id=CORR
        )
    denied = [
        e for e in service.audit_trail(app.application_id) if e.outcome is AuditOutcome.DENIED
    ]
    assert [(e.event_type, e.actor_id) for e in denied] == [
        ("application.access_denied", "agent-b")
    ]


def test_missing_application_is_not_found(service: ApplicationService) -> None:
    reader = status_principal(service, create(service))
    with pytest.raises(NotFoundError):
        service.get_application(principal=reader, application_id="app-nope", correlation_id=CORR)


# --- transitions ------------------------------------------------------------


def test_full_happy_path_is_persisted_with_one_audit_event_per_step(
    service: ApplicationService,
) -> None:
    app = create(service).application
    for expected_version, (target, authority) in enumerate(HAPPY_PATH, start=2):
        updated = move(service, app.application_id, target, authority, "step")
        assert (updated.status, updated.version) == (target, expected_version)
    events = service.audit_trail(app.application_id)
    assert [e.event_type for e in events] == ["application.created"] + [
        "application.transition"
    ] * 7
    assert [e.details["to"] for e in events[1:]] == [t.value for t, _ in HAPPY_PATH]
    assert events[-1].actor_type is AuditActorType.PROVIDER
    assert all(e.outcome is AuditOutcome.SUCCESS for e in events)


def test_invalid_transition_is_rejected_unchanged_and_audited(
    service: ApplicationService,
) -> None:
    app = create(service).application
    with pytest.raises(InvalidTransitionError):
        move(service, app.application_id, S.CONTRACT_PENDING, A.SYSTEM_POLICY)
    current = service.snapshot(app.application_id)
    assert (current.status, current.version) == (S.DRAFT, 1)
    rejected = service.audit_trail(app.application_id)[-1]
    assert rejected.event_type == "application.transition_rejected"
    assert rejected.outcome is AuditOutcome.DENIED
    assert rejected.details == {
        "from": "DRAFT",
        "to": "CONTRACT_PENDING",
        "authority": "SYSTEM_POLICY",
        "reason": "invalid_transition",
    }


@pytest.mark.parametrize("target", [t for t in S if t is not S.DRAFT])
def test_the_agent_authority_can_never_change_state(service: ApplicationService, target: S) -> None:
    app = create(service).application
    with pytest.raises((InvalidTransitionError, TransitionNotAuthorizedError)):
        move(service, app.application_id, target, A.AGENT)
    unchanged = service.snapshot(app.application_id)
    assert unchanged.status is S.DRAFT


def test_direct_opened_is_rejected_from_every_reachable_state(
    service: ApplicationService,
) -> None:
    """OPENED requires SUBMITTED_TO_DEPOT_PROVIDER *and* the provider-confirmation authority."""
    app = create(service).application
    for target, authority in HAPPY_PATH[:-1]:
        for bad_authority in A:
            with pytest.raises((InvalidTransitionError, TransitionNotAuthorizedError)):
                move(service, app.application_id, S.OPENED, bad_authority)
        move(service, app.application_id, target, authority)
    # now SUBMITTED_TO_DEPOT_PROVIDER: only the trusted confirmation works
    for bad_authority in (a for a in A if a is not A.DEPOT_PROVIDER_CONFIRMATION):
        with pytest.raises(TransitionNotAuthorizedError):
            move(service, app.application_id, S.OPENED, bad_authority)
    assert (
        move(service, app.application_id, S.OPENED, A.DEPOT_PROVIDER_CONFIRMATION).status
        is S.OPENED
    )


def test_terminal_state_cannot_be_left(service: ApplicationService) -> None:
    app = create(service).application
    move(service, app.application_id, S.CANCELLED, A.CUSTOMER_CHANNEL)
    for target in S:
        for authority in A:
            with pytest.raises((InvalidTransitionError, TransitionNotAuthorizedError)):
                move(service, app.application_id, target, authority)


def test_manual_review_remembers_and_resumes_the_interrupted_step(
    service: ApplicationService,
) -> None:
    app = create(service).application
    for target, authority in HAPPY_PATH[:4]:  # up to COMPLIANCE_PENDING
        move(service, app.application_id, target, authority)
    parked = move(service, app.application_id, S.MANUAL_REVIEW, A.SYSTEM_POLICY, "review_required")
    assert (parked.status, parked.resume_status) == (S.MANUAL_REVIEW, S.COMPLIANCE_PENDING)

    with pytest.raises(InvalidTransitionError):  # cannot skip the compliance check
        move(service, app.application_id, S.CONTRACT_PENDING, A.OPERATOR)
    with pytest.raises(TransitionNotAuthorizedError):  # the agent/system cannot self-release
        move(service, app.application_id, S.COMPLIANCE_PENDING, A.SYSTEM_POLICY)

    resumed = move(service, app.application_id, S.COMPLIANCE_PENDING, A.OPERATOR, "operator_ok")
    assert (resumed.status, resumed.resume_status) == (S.COMPLIANCE_PENDING, None)


def test_transition_on_unknown_application_is_not_found(service: ApplicationService) -> None:
    with pytest.raises(NotFoundError):
        move(service, "app-nope", S.CANCELLED, A.SYSTEM_POLICY)


def test_reason_code_must_be_a_machine_code(service: ApplicationService) -> None:
    app = create(service).application
    with pytest.raises(ValueError, match="reason_code"):
        move(service, app.application_id, S.CANCELLED, A.SYSTEM_POLICY, "Free text, with PII!")


def test_audit_details_contain_no_idempotency_keys_or_customer_data(
    service: ApplicationService,
) -> None:
    key = new_key()
    app = create(service, key=key).application
    move(service, app.application_id, S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY, "handoff_created")
    blob = json.dumps([e.model_dump(mode="json") for e in service.audit_trail(app.application_id)])
    assert key not in blob
    assert "opaque-id" not in blob  # customer_ref is never copied into the audit log


# --- transaction consistency ------------------------------------------------------------


def test_failed_audit_write_rolls_back_the_status_change(
    service: ApplicationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create(service).application
    original = SqlAlchemyAuditRepository.append

    def failing_append(self: SqlAlchemyAuditRepository, event) -> None:  # noqa: ANN001
        if event.event_type == "application.transition":
            raise RuntimeError("audit store unavailable")
        original(self, event)

    monkeypatch.setattr(SqlAlchemyAuditRepository, "append", failing_append)
    with pytest.raises(RuntimeError, match="audit store unavailable"):
        move(service, app.application_id, S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY)
    monkeypatch.undo()

    current = service.snapshot(app.application_id)
    assert (current.status, current.version) == (S.DRAFT, 1)  # no change without its audit event
    assert [e.event_type for e in service.audit_trail(app.application_id)] == [
        "application.created"
    ]


def test_failed_audit_write_leaves_no_application_or_idempotency_record(
    service: ApplicationService, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_append(self: SqlAlchemyAuditRepository, event) -> None:  # noqa: ANN001
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(SqlAlchemyAuditRepository, "append", failing_append)
    key = new_key()
    with pytest.raises(RuntimeError):
        create(service, key=key)
    monkeypatch.undo()

    assert (count(engine, "applications"), count(engine, "idempotency_records")) == (0, 0)
    assert create(service, key=key).replayed is False  # the key was not burned


# --- duplicate / concurrent submissions -----------------------------------------------------


# --- forced race interleavings (deterministic) ----------------------------------------------
#
# Thread tests alone can pass because the threads happen to run one after another. These tests force
# the exact interleaving of a real race: the loser's *read* is stale (it saw "nothing yet"), so only
# the database constraint / compare-and-set update stands between it and a duplicate or lost update.


def stale_once(
    monkeypatch: pytest.MonkeyPatch, cls: type, method: str, stale_value: object
) -> None:
    original = getattr(cls, method)
    calls = {"n": 0}

    def wrapper(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        calls["n"] += 1
        return stale_value if calls["n"] == 1 else original(self, *args, **kwargs)

    monkeypatch.setattr(cls, method, wrapper)


def test_unique_constraint_stops_a_duplicate_when_the_idempotency_check_raced(
    service: ApplicationService, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.sqlalchemy_repositories import SqlAlchemyIdempotencyRepository

    key = new_key()
    winner = create(service, key=key).application
    stale_once(monkeypatch, SqlAlchemyIdempotencyRepository, "get", None)  # loser sees "no record"

    loser = create(service, key=key)  # tries to insert -> UNIQUE violation -> rolled back -> replay
    assert loser.replayed is True
    assert loser.application == winner
    assert (count(engine, "applications"), count(engine, "idempotency_records")) == (1, 1)
    assert application_events(engine) == 1  # the losing attempt left nothing behind


def test_unique_constraint_race_with_a_different_body_is_a_conflict_not_a_second_draft(
    service: ApplicationService, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.sqlalchemy_repositories import SqlAlchemyIdempotencyRepository

    key = new_key()
    create(service, key=key)
    stale_once(monkeypatch, SqlAlchemyIdempotencyRepository, "get", None)

    with pytest.raises(IdempotencyConflictError):
        create(service, cmd=OTHER_CMD, key=key)
    assert count(engine, "applications") == 1


def test_compare_and_set_stops_a_lost_update_when_the_status_read_raced(
    service: ApplicationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.sqlalchemy_repositories import SqlAlchemyApplicationRepository

    app = create(service).application
    move(
        service, app.application_id, S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY
    )  # winner got in first
    stale_once(
        monkeypatch, SqlAlchemyApplicationRepository, "get", app
    )  # loser still sees DRAFT v1

    with pytest.raises(ConcurrentModificationError):
        move(service, app.application_id, S.CANCELLED, A.CUSTOMER_CHANNEL)
    monkeypatch.undo()

    final = service.snapshot(app.application_id)
    assert (final.status, final.version) == (
        S.AWAITING_AUTHORIZATION,
        2,
    )  # the loser changed nothing
    types = [e.event_type for e in service.audit_trail(app.application_id)]
    assert types == ["application.created", "application.transition"]  # and wrote no audit event


def test_version_counter_catches_an_aba_change_the_status_check_alone_would_miss(
    service: ApplicationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.sqlalchemy_repositories import SqlAlchemyApplicationRepository

    app = create(service).application
    for target, authority in HAPPY_PATH[:3]:  # ... -> KYC_PENDING
        move(service, app.application_id, target, authority)
    snapshot = service.snapshot(app.application_id)
    assert snapshot.status is S.KYC_PENDING
    # Meanwhile another writer parks the application and resumes it: the status is KYC_PENDING
    # again (A -> B -> A), but it is a different version of the record.
    move(service, app.application_id, S.ACTION_REQUIRED, A.SYSTEM_POLICY)
    move(service, app.application_id, S.KYC_PENDING, A.CUSTOMER_CHANNEL)

    stale_once(monkeypatch, SqlAlchemyApplicationRepository, "get", snapshot)
    with pytest.raises(ConcurrentModificationError):
        move(service, app.application_id, S.COMPLIANCE_PENDING, A.SYSTEM_POLICY)


def run_concurrently(n: int, fn: Callable[[int], object]) -> list[object]:
    barrier = threading.Barrier(n)

    def worker(i: int) -> object:
        barrier.wait()
        try:
            return fn(i)
        except Exception as exc:  # noqa: BLE001 - collected and asserted on by the tests
            return exc

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(worker, range(n)))


def test_concurrent_identical_creates_yield_exactly_one_application(
    service: ApplicationService, engine: Engine
) -> None:
    key = new_key()
    results = run_concurrently(8, lambda _: create(service, key=key))

    assert not [r for r in results if isinstance(r, Exception)], results
    assert len({r.application.application_id for r in results}) == 1  # type: ignore[union-attr]
    assert sorted(r.replayed for r in results) == [False] + [True] * 7  # type: ignore[union-attr]
    assert count(engine, "applications") == 1
    assert count(engine, "idempotency_records") == 1
    assert application_events(engine) == 1


def test_concurrent_creates_with_the_same_key_but_different_bodies_allow_only_one(
    service: ApplicationService, engine: Engine
) -> None:
    key = new_key()
    results = run_concurrently(
        8, lambda i: create(service, cmd=CMD if i % 2 else OTHER_CMD, key=key)
    )

    created = [r for r in results if not isinstance(r, Exception) and not r.replayed]  # type: ignore[union-attr]
    assert len(created) == 1
    assert all(
        isinstance(r, IdempotencyConflictError) or not isinstance(r, Exception) for r in results
    )
    assert count(engine, "applications") == 1


def test_concurrent_creates_with_distinct_keys_do_not_interfere(
    service: ApplicationService, engine: Engine
) -> None:
    results = run_concurrently(8, lambda _: create(service))
    assert not [r for r in results if isinstance(r, Exception)], results
    assert count(engine, "applications") == 8


def test_concurrent_transitions_let_exactly_one_writer_win(
    service: ApplicationService, engine: Engine
) -> None:
    app = create(service).application
    results = run_concurrently(
        8,
        lambda _: move(service, app.application_id, S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY),
    )
    winners = [r for r in results if not isinstance(r, Exception)]
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == 1
    assert all(isinstance(e, ConflictError) for e in losers), losers  # 409, never a 500
    final = service.snapshot(app.application_id)
    assert (final.status, final.version) == (S.AWAITING_AUTHORIZATION, 2)
    successes = [
        e
        for e in service.audit_trail(app.application_id)
        if e.event_type == "application.transition"
    ]
    assert len(successes) == 1  # one state change, one audit event


def test_two_conflicting_concurrent_transitions_always_end_in_a_valid_chain(
    service: ApplicationService,
) -> None:
    app = create(service).application
    targets = [(S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY), (S.CANCELLED, A.CUSTOMER_CHANNEL)]
    results = run_concurrently(2, lambda i: move(service, app.application_id, *targets[i]))

    assert all(not isinstance(r, Exception) or isinstance(r, ConflictError) for r in results)
    successes = [r for r in results if not isinstance(r, Exception)]
    final = service.snapshot(app.application_id)
    assert final.version == 1 + len(successes)
    # Both ran: DRAFT -> AWAITING_AUTHORIZATION -> CANCELLED. One ran: that writer's target.
    # (CANCELLED -> AWAITING_AUTHORIZATION is impossible, so no other combination exists.)
    assert final.status is (S.CANCELLED if len(successes) == 2 else successes[0].status)


def test_unit_of_work_factory_type_is_the_interface(uow_factory: Callable[[], UnitOfWork]) -> None:
    with uow_factory() as uow:
        assert (
            hasattr(uow, "applications") and hasattr(uow, "audit") and hasattr(uow, "idempotency")
        )
