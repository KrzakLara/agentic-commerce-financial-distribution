from datetime import datetime

from fastapi.testclient import TestClient

from app import __version__


def test_health_returns_200(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_health_returns_expected_json_structure(client: TestClient) -> None:
    response = client.get("/health")
    body = response.json()

    assert response.headers["content-type"] == "application/json"
    assert set(body) == {"status", "service", "version", "api_version", "environment", "timestamp"}
    assert body["status"] == "ok"
    assert body["service"] == "TRUSTEQ Secure Agentic Depot Onboarding"
    assert body["version"] == __version__
    assert body["api_version"] == "v1"
    assert body["environment"] == "test"
    assert datetime.fromisoformat(body["timestamp"]).tzinfo is not None


def test_health_exposes_no_configuration_secrets(client: TestClient) -> None:
    text = client.get("/health").text.lower()
    for forbidden in ("database", "api_key", "password", "secret", "sqlite"):
        assert forbidden not in text


def test_every_response_carries_a_correlation_id(client: TestClient) -> None:
    generated = client.get("/health").headers["x-correlation-id"]
    assert len(generated) == 32

    echoed = client.get("/health", headers={"X-Correlation-ID": "demo-123"})
    assert echoed.headers["x-correlation-id"] == "demo-123"


def test_unsafe_correlation_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Correlation-ID": "bad id with spaces\t!"})
    assert response.headers["x-correlation-id"] != "bad id with spaces\t!"
    assert len(response.headers["x-correlation-id"]) == 32
