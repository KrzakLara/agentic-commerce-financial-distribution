"""Phase 00 acceptance: import, initialization, startup without AI keys."""

import importlib
import tomllib
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as app_package
from app.core.config import Environment, Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_application_imports_successfully() -> None:
    for module in ("app.main", "app.api.router", "app.core.config", "app.domain", "app.agents"):
        assert importlib.import_module(module) is not None


def test_fastapi_application_initializes(app: FastAPI) -> None:
    assert isinstance(app, FastAPI)
    assert app.title == "TRUSTEQ Secure Agentic Depot Onboarding"
    assert app.version == app_package.__version__
    assert "/health" in app.openapi()["paths"]


def test_package_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == app_package.__version__


def test_application_starts_without_ai_api_keys(client: TestClient) -> None:
    # The autouse fixture removed every TRUSTEQ_* variable: no key, no provider configured.
    assert client.get("/health").status_code == 200
    assert client.app.state.settings.llm_api_key is None


def test_openapi_documentation_is_served(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "TRUSTEQ Secure Agentic Depot Onboarding"
    assert "/health" in schema["paths"]
    assert client.get("/docs").status_code == 200


def test_openapi_docs_are_disabled_in_production() -> None:
    prod = create_app(Settings(_env_file=None, environment=Environment.PRODUCTION))
    with TestClient(prod) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/health").status_code == 200
