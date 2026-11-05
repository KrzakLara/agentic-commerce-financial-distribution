"""The standalone synthetic tax-information mock: typed, validated, kept out of logs and models."""

from pathlib import Path

import pytest

from app.integrations.agent_tools import AgentToolName
from app.services.tax_information import (
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
    TaxDataSource,
    TaxFieldStatus,
    validate_tax_information,
)

APP = Path(__file__).resolve().parent.parent / "app"
CUSTOMER = TaxDataSource.CUSTOMER_ON_BANK_CHANNEL
SYNTHETIC_ID = "SYNTH-TAX-000123"
COMPLETE = {
    "tax_residency_country": "DE",
    "synthetic_tax_id": SYNTHETIC_ID,
    "us_person_declared": False,
}


def test_a_complete_synthetic_submission_is_collected() -> None:
    result = validate_tax_information(COMPLETE, CUSTOMER)
    assert result.complete and result.record is not None
    assert all(result.statuses[name] is TaxFieldStatus.COLLECTED for name in REQUIRED_FIELDS)
    assert all(
        result.statuses[name] is TaxFieldStatus.NOT_PROVIDED_OPTIONAL for name in OPTIONAL_FIELDS
    )
    assert result.record.synthetic_tax_id.get_secret_value() == SYNTHETIC_ID


def test_optional_fields_are_validated_when_given() -> None:
    result = validate_tax_information(
        {**COMPLETE, "exemption_order_amount_eur": 500, "church_tax_objection": True}, CUSTOMER
    )
    assert result.complete and result.record is not None
    assert result.record.exemption_order_amount_eur == 500
    invalid = validate_tax_information({**COMPLETE, "exemption_order_amount_eur": -1}, CUSTOMER)
    assert invalid.statuses["exemption_order_amount_eur"] is TaxFieldStatus.INVALID
    assert not invalid.complete


@pytest.mark.parametrize("name", REQUIRED_FIELDS)
def test_a_missing_required_field_is_reported_as_missing(name: str) -> None:
    result = validate_tax_information({k: v for k, v in COMPLETE.items() if k != name}, CUSTOMER)
    assert result.statuses[name] is TaxFieldStatus.MISSING
    assert not result.complete and result.record is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("tax_residency_country", "Germany"),
        ("tax_residency_country", "de"),
        ("tax_residency_country", 49),
        ("synthetic_tax_id", "12345678901"),  # shaped like a real German tax ID
        ("synthetic_tax_id", "12 345 678 901"),
        ("synthetic_tax_id", "SYNTH-TAX-12"),
        ("us_person_declared", "no"),
        ("us_person_declared", 0),
    ],
)
def test_an_invalid_field_is_reported_as_invalid_and_never_echoed(name: str, value: object) -> None:
    result = validate_tax_information({**COMPLETE, name: value}, CUSTOMER)
    assert result.statuses[name] is TaxFieldStatus.INVALID
    assert not result.complete
    dump = repr(result) + str(result.audit_details()) + result.model_dump_json()
    if isinstance(value, str) and len(value) > 3:  # shorter ones occur inside field names
        assert value not in dump


def test_data_from_an_agent_or_a_model_is_never_authoritative() -> None:
    result = validate_tax_information(COMPLETE, TaxDataSource.AGENT_OR_MODEL)
    assert not result.complete and result.record is None
    assert all(result.statuses[name] is TaxFieldStatus.INVALID for name in REQUIRED_FIELDS)


def test_the_identifier_stays_out_of_audit_details_repr_and_json() -> None:
    result = validate_tax_information(COMPLETE, CUSTOMER)
    assert result.audit_details()["synthetic_tax_id"] == "COLLECTED"
    assert result.audit_details()["complete"] == "true"
    for dump in (str(result.audit_details()), repr(result), result.model_dump_json()):
        assert SYNTHETIC_ID not in dump


def test_no_agent_tool_or_model_code_reaches_tax_data() -> None:
    assert not any("tax" in tool.value for tool in AgentToolName)
    for module in ("agents/llm.py", "agents/scripted.py", "integrations/rest_gateway.py"):
        source = (APP / module).read_text(encoding="utf-8").lower()
        assert "tax" not in source.replace("syntax", "")
