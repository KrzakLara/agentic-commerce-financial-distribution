"""Versioned contract of the MOCK depot provider's submission API (synthetic v1 and v2).

`VersionedDepotAdapter` implements the existing `DepotProviderPort`, so nothing above that
boundary knows a version exists: the bank's policy gate still decides whether to call it, the
state machine still decides what follows, and OPENED still comes only from the provider's signed
callback. It is NOT wired into the default container: the demos keep `MockDepotProvider`. Wiring
it in means setting `AppContainer.depot`, as the contract tests do.

Rules of this contract:

* The bank asks for the newest version it supports and falls back only when the provider states
  that a version is gone (`ProviderVersionUnavailable`, like HTTP 410). The provider did not
  process that request, so asking again in an older version cannot create a second hand-over.
* A malformed, unexpected or wrong-version answer is NEVER retried in another version: the
  provider may have acted on it. It becomes a refused submission, which the onboarding service
  audits, leaving the application where it was.
* An answer can only say accepted or not. Fields it does not define, such as ``opened``, make it
  invalid, because no synchronous answer may open an account (ADR section 05).

The signed confirmation callback is not versioned here: it stays as it is.
"""

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from app.core.logging import get_logger
from app.domain.application import DepotApplication
from app.domain.base import OpaqueId
from app.integrations.results import ProviderSubmission

ContractVersion = Literal["v1", "v2"]
SUPPORTED_VERSIONS: tuple[ContractVersion, ...] = ("v2", "v1")  # the bank's preference order
NO_SUBMISSION_REF = "NO-SUBMISSION"

_logger = get_logger(__name__)


class ProviderVersionUnavailable(Exception):
    """The provider states that it no longer serves this version; the request was not processed."""


class ProviderContractViolation(Exception):
    """The answer does not satisfy the contract. Carries no provider content."""


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SubmissionResponseV1(_Wire):
    contract_version: Literal["v1"]
    provider_ref: OpaqueId
    accepted: StrictBool


class _SubmissionV2(_Wire):
    reference: OpaqueId
    status: Literal["ACCEPTED", "DECLINED"]


class SubmissionResponseV2(_Wire):
    contract_version: Literal["v2"]
    submission: _SubmissionV2


def build_submission_request(version: ContractVersion, application: DepotApplication) -> dict:
    """What the bank sends: references only, keyed by application so a repeat is recognizable."""
    fields = {
        "application_id": application.application_id,
        "provider_id": application.provider_id,
        "product_id": application.product_id,
        "channel_id": application.channel_id,
    }
    if version == "v1":
        return {"contract_version": "v1", **fields}
    return {
        "contract_version": "v2",
        "idempotency_key": application.application_id,
        "application": fields,
    }


def parse_submission_response(
    version: ContractVersion, payload: Mapping[str, Any]
) -> ProviderSubmission:
    """Validate a provider answer for the version that was asked for, or raise."""
    try:
        if version == "v1":
            v1 = SubmissionResponseV1.model_validate(payload)
            return ProviderSubmission(provider_ref=v1.provider_ref, accepted=v1.accepted, mock=True)
        if version == "v2":
            v2 = SubmissionResponseV2.model_validate(payload)
            return ProviderSubmission(
                provider_ref=v2.submission.reference,
                accepted=v2.submission.status == "ACCEPTED",
                mock=True,
            )
    except ValidationError:
        raise ProviderContractViolation(f"invalid {version} submission response") from None
    raise ProviderContractViolation("unsupported contract version")


class DepotTransport(Protocol):
    def __call__(self, version: ContractVersion, request: dict) -> Mapping[str, Any]: ...


class VersionedDepotAdapter:
    """`DepotProviderPort` over a versioned transport. See the module docstring for the rules."""

    IS_MOCK: ClassVar[bool] = True

    def __init__(
        self,
        transport: DepotTransport,
        *,
        versions: tuple[str, ...] = SUPPORTED_VERSIONS,
        on_event: Callable[[str, str, str], None] | None = None,
    ) -> None:
        unknown = set(versions) - set(SUPPORTED_VERSIONS)
        if unknown or not versions:
            raise ValueError("the adapter can only be configured with supported versions")
        self._transport = transport
        self._versions: tuple[ContractVersion, ...] = versions  # type: ignore[assignment]
        self._on_event = on_event or (lambda _application, _version, _event: None)

    def submit_application(self, application: DepotApplication) -> ProviderSubmission:
        app_id = application.application_id
        for version in self._versions:
            try:
                answer = self._transport(version, build_submission_request(version, application))
            except ProviderVersionUnavailable:
                self._event(app_id, version, "version_unavailable")
                continue
            try:
                submission = parse_submission_response(version, answer)
            except ProviderContractViolation:
                self._event(app_id, version, "contract_violation")
                return self._refused()  # no fallback: the provider may have acted on the request
            self._event(app_id, version, "answered")
            return submission
        self._event(app_id, "-", "no_supported_version")
        return self._refused()

    def _event(self, application_id: str, version: str, event: str) -> None:
        if event != "answered":
            _logger.warning("depot contract: %s (%s) for %s", event, version, application_id)
        self._on_event(application_id, version, event)

    @staticmethod
    def _refused() -> ProviderSubmission:
        return ProviderSubmission(provider_ref=NO_SUBMISSION_REF, accepted=False, mock=True)


class MockVersionedDepotApi:
    """The MOCK provider's side of the contract: synthetic, deterministic, in-process."""

    IS_MOCK: ClassVar[bool] = True

    def __init__(self, served: tuple[str, ...] = ("v1", "v2")) -> None:
        self.served = served
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, version: ContractVersion, request: dict) -> Mapping[str, Any]:
        if version not in self.served:
            raise ProviderVersionUnavailable(version)
        self.calls.append((version, request))
        if version == "v1":
            reference = f"MOCK-DEPOT-{request['application_id']}"
            return {"contract_version": "v1", "provider_ref": reference, "accepted": True}
        reference = f"MOCK-DEPOT-{request['application']['application_id']}"
        return {
            "contract_version": "v2",
            "submission": {"reference": reference, "status": "ACCEPTED"},
        }
