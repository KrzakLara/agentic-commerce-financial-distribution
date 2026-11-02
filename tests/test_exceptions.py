"""Basic exception handling produces the consistent JSON error envelope."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.exceptions import ConflictError, NotFoundError, PolicyDeniedError


class Payload(BaseModel):
    quantity: int


@pytest.fixture
def error_app(app: FastAPI) -> FastAPI:
    """The real app plus throw-away routes that raise on purpose (test-only)."""

    @app.get("/_test/not-found")
    def not_found() -> None:
        raise NotFoundError()

    @app.get("/_test/conflict")
    def conflict() -> None:
        raise ConflictError("custom conflict message")

    @app.get("/_test/denied")
    def denied() -> None:
        raise PolicyDeniedError()

    @app.get("/_test/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail: db password=hunter2")

    @app.post("/_test/validate")
    def validate(payload: Payload) -> Payload:
        return payload

    return app


@pytest.fixture
def error_client(error_app: FastAPI) -> TestClient:
    return TestClient(error_app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/_test/not-found", 404, "not_found"),
        ("/_test/conflict", 409, "conflict"),
        ("/_test/denied", 403, "policy_denied"),
    ],
)
def test_application_errors_use_error_envelope(
    error_client: TestClient, path: str, status: int, code: str
) -> None:
    response = error_client.get(path)
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["message"]
    assert error["correlation_id"] == response.headers["x-correlation-id"]


def test_custom_message_is_returned(error_client: TestClient) -> None:
    assert (
        error_client.get("/_test/conflict").json()["error"]["message"] == "custom conflict message"
    )


def test_unknown_route_uses_error_envelope(error_client: TestClient) -> None:
    response = error_client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_404"


def test_wrong_method_uses_error_envelope(error_client: TestClient) -> None:
    response = error_client.post("/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "http_405"


def test_validation_error_does_not_echo_input(error_client: TestClient) -> None:
    response = error_client.post("/_test/validate", json={"quantity": "SENSITIVE-VALUE"})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"][0]["loc"] == ["body", "quantity"]
    assert "SENSITIVE-VALUE" not in response.text


def test_unhandled_exception_returns_generic_500_and_leaks_nothing(
    error_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.errors"):
        response = error_client.get("/_test/boom")

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert "Traceback" not in response.text
    assert body["correlation_id"] == response.headers["x-correlation-id"]
    # The detail is logged server-side for operators.
    assert any("unhandled exception" in record.getMessage() for record in caplog.records)
