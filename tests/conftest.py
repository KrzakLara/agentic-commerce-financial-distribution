import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from app.container import build_default_container
from app.core.config import Environment, Settings
from app.main import create_app
from app.repositories.database import create_db_engine, create_session_factory, init_database
from app.repositories.interfaces import UnitOfWork
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.application_service import ApplicationService

if TYPE_CHECKING:
    from tests.flow import Flow

DEV_ACTOR_HEADER = "X-Dev-Actor-Id"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tests independent of the developer's shell variables and local .env file.

    Also points the default database at memory so no test can create ./trusteq.db.
    """
    for name in list(os.environ):
        if name.startswith("TRUSTEQ_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRUSTEQ_DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, environment=Environment.TEST)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def actor(actor_id: str) -> dict[str, str]:
    return {DEV_ACTOR_HEADER: actor_id}


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'trusteq_test.db').as_posix()}"


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    """A real SQLite *file* database (needed for restart and concurrency tests)."""
    db_engine = create_db_engine(db_url)
    init_database(db_engine)
    yield db_engine
    db_engine.dispose()


@pytest.fixture
def uow_factory(engine: Engine) -> Callable[[], UnitOfWork]:
    session_factory = create_session_factory(engine)
    return lambda: SqlAlchemyUnitOfWork(session_factory)


@pytest.fixture
def service(uow_factory: Callable[[], UnitOfWork]) -> ApplicationService:
    return ApplicationService(uow_factory)


@pytest.fixture
def flow_factory(db_url: str) -> Iterator[Callable[..., "Flow"]]:
    """Build a `Flow` (agent / customer / provider clients on one file-backed app).

    Call with overrides to swap in mock outcomes, e.g. ``flow_factory(compliance=...)``.
    """
    from tests.flow import PROVIDER_SECRET, FakeClock, Flow

    opened: list[TestClient] = []

    def build(**overrides: object) -> Flow:
        clock = FakeClock()
        settings = Settings(
            _env_file=None,
            environment=Environment.TEST,
            database_url=db_url,
            mock_provider_callback_secret=SecretStr(PROVIDER_SECRET),
            **{k: v for k, v in overrides.items() if k in Settings.model_fields},  # type: ignore[arg-type]
        )
        container = build_default_container(settings)
        container.clock = clock
        for name in ("catalog", "kyc", "compliance", "contract", "depot"):
            if name in overrides:
                setattr(container, name, overrides[name])
        app = create_app(settings, container)
        main = TestClient(app)
        main.__enter__()  # runs the lifespan once (creates the schema)
        opened.append(main)
        return Flow(
            app=app,
            agent_http=TestClient(app),
            customer_http=TestClient(app),
            provider_http=TestClient(app),
            clock=clock,
            container=container,
            engine_getter=lambda: app.state.engine,
        )

    yield build
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def flow(flow_factory: Callable[..., "Flow"]) -> "Flow":
    return flow_factory()
