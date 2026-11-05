"""Standalone MOCK of onboarding tax information: a typed contract and deterministic validation.

SYNTHETIC data only: the identifier format below is invented, so a value shaped like a real
German tax ID (11 digits) fails validation. There is no connection to any tax authority. The
field list is what this prototype models, not a statement of what the law requires. It is not
called by the frozen onboarding flow.

Intended integration point: collected by the customer on the bank's own page, after the sign-in
and before the KYC step, with the status summary as evidence for the policy gate. The agent has no
tool for it, so tax data never enters model traffic. Anything an agent or a model supplies is
never authoritative. Audit records get the status summary from `audit_details`, never the
identifier.
"""

import re
from collections.abc import Mapping
from enum import StrEnum

from pydantic import SecretStr

from app.domain.base import DomainModel

SYNTHETIC_TAX_ID_FORMAT = re.compile(r"^SYNTH-TAX-\d{6}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
REQUIRED_FIELDS = ("tax_residency_country", "synthetic_tax_id", "us_person_declared")
OPTIONAL_FIELDS = ("exemption_order_amount_eur", "church_tax_objection")


class TaxDataSource(StrEnum):
    CUSTOMER_ON_BANK_CHANNEL = "CUSTOMER_ON_BANK_CHANNEL"
    AGENT_OR_MODEL = "AGENT_OR_MODEL"


class TaxFieldStatus(StrEnum):
    COLLECTED = "COLLECTED"
    MISSING = "MISSING"
    INVALID = "INVALID"
    NOT_PROVIDED_OPTIONAL = "NOT_PROVIDED_OPTIONAL"


class SyntheticTaxRecord(DomainModel):
    """Validated synthetic values. The identifier stays hidden in repr and in JSON dumps."""

    tax_residency_country: str
    synthetic_tax_id: SecretStr
    us_person_declared: bool
    exemption_order_amount_eur: int | None = None  # Freistellungsauftrag: amount not checked
    church_tax_objection: bool | None = None  # Sperrvermerk


class TaxValidationResult(DomainModel):
    statuses: dict[str, TaxFieldStatus]
    source: TaxDataSource
    record: SyntheticTaxRecord | None = None  # only if complete and from the customer

    @property
    def complete(self) -> bool:
        return self.record is not None

    def audit_details(self) -> dict[str, str]:
        """What may enter an audit event: field statuses only, no value of any field."""
        return {
            "source": self.source.value,
            "complete": str(self.complete).lower(),
            **{name: status.value for name, status in self.statuses.items()},
        }


def _check(name: str, value: object) -> TaxFieldStatus:
    if name == "tax_residency_country":
        valid = isinstance(value, str) and _COUNTRY.fullmatch(value) is not None
    elif name == "synthetic_tax_id":
        valid = isinstance(value, str) and SYNTHETIC_TAX_ID_FORMAT.fullmatch(value) is not None
    elif name == "exemption_order_amount_eur":
        valid = type(value) is int and value >= 0
    else:  # us_person_declared, church_tax_objection
        valid = type(value) is bool
    return TaxFieldStatus.COLLECTED if valid else TaxFieldStatus.INVALID


def validate_tax_information(
    submitted: Mapping[str, object], source: TaxDataSource
) -> TaxValidationResult:
    """Sort every field into collected, missing or invalid. Unknown fields are ignored and dropped.

    Refused values are never echoed anywhere in the result.
    """
    statuses: dict[str, TaxFieldStatus] = {}
    for name in (*REQUIRED_FIELDS, *OPTIONAL_FIELDS):
        value = submitted.get(name)
        if value is None or value == "":
            statuses[name] = (
                TaxFieldStatus.MISSING
                if name in REQUIRED_FIELDS
                else TaxFieldStatus.NOT_PROVIDED_OPTIONAL
            )
        elif source is not TaxDataSource.CUSTOMER_ON_BANK_CHANNEL:
            statuses[name] = TaxFieldStatus.INVALID  # never authoritative, however well formed
        else:
            statuses[name] = _check(name, value)

    usable = source is TaxDataSource.CUSTOMER_ON_BANK_CHANNEL and not any(
        status in {TaxFieldStatus.MISSING, TaxFieldStatus.INVALID} for status in statuses.values()
    )
    record = (
        SyntheticTaxRecord(
            **{
                name: submitted[name]
                for name, status in statuses.items()
                if status is TaxFieldStatus.COLLECTED
            }
        )
        if usable
        else None
    )
    return TaxValidationResult(statuses=statuses, source=source, record=record)
