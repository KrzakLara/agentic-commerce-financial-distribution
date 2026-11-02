import pytest
from pydantic import ValidationError

from app.core.config import AIExecutionMode, Environment, Settings, get_settings


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_are_safe_and_need_no_secrets() -> None:
    s = make()
    assert s.environment is Environment.DEVELOPMENT
    assert s.debug is False
    assert s.api_version == "v1"
    assert s.database_url.startswith("sqlite")
    assert s.log_level == "INFO"
    assert s.ai_execution_mode is AIExecutionMode.SCRIPTED
    assert s.enable_llm is False
    assert s.llm_provider is None
    assert s.llm_api_key is None
    assert "*" not in s.allowed_origins


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTEQ_ENVIRONMENT", "test")
    monkeypatch.setenv("TRUSTEQ_LOG_LEVEL", "debug")
    monkeypatch.setenv("TRUSTEQ_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("TRUSTEQ_ALLOWED_ORIGINS", '["https://frontend.example"]')
    s = make()
    assert s.environment is Environment.TEST
    assert s.log_level == "DEBUG"
    assert s.database_url == "sqlite:///:memory:"
    assert s.allowed_origins == ["https://frontend.example"]


def test_blank_optional_values_from_env_example_mean_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY"):
        monkeypatch.setenv(f"TRUSTEQ_LLM_{name}", "")
    s = make()
    assert s.llm_provider is None
    assert s.llm_api_key is None


def test_api_key_is_never_shown_in_repr() -> None:
    from pydantic import SecretStr

    s = make(llm_api_key=SecretStr("sk-test-not-real"))
    assert "sk-test-not-real" not in repr(s)
    assert "sk-test-not-real" not in str(s.model_dump())


def test_llm_mode_requires_explicit_flag_and_provider() -> None:
    with pytest.raises(ValidationError, match="enable_llm"):
        make(ai_execution_mode="llm")
    with pytest.raises(ValidationError, match="llm_provider"):
        make(ai_execution_mode="llm", enable_llm=True)
    assert make(ai_execution_mode="llm", enable_llm=True, llm_provider="example").enable_llm


def test_production_rejects_debug_and_wildcard_origin() -> None:
    with pytest.raises(ValidationError, match="debug"):
        make(environment="production", debug=True)
    with pytest.raises(ValidationError, match="wildcard"):
        make(environment="production", allowed_origins=["*"])


def test_invalid_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make(environment="staging")
    with pytest.raises(ValidationError):
        make(log_level="LOUD")


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()


def test_dev_actor_identification_is_never_active_in_production() -> None:
    dev = make(environment="development")
    assert dev.dev_actor_auth_enabled and dev.dev_actor_auth_active
    prod = make(environment="production")  # default value is ignored in production
    assert prod.dev_actor_auth_enabled and not prod.dev_actor_auth_active
    with pytest.raises(ValidationError, match="dev_actor_auth_enabled"):
        make(environment="production", dev_actor_auth_enabled=True)  # explicit opt-in is refused
    assert not make(environment="production", dev_actor_auth_enabled=False).dev_actor_auth_active
    assert not make(environment="test", dev_actor_auth_enabled=False).dev_actor_auth_active
