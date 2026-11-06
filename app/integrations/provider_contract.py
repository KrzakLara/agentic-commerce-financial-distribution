"""Versioned contract of the MOCK depot provider's submission API (synthetic v1 and v2).

`VersionedDepotAdapter` implements the existing `DepotProviderPort` and is what the default
container uses, so every submission of the real application passes through this contract. Nothing
above that boundary knows a version exists: the bank's policy gate still decides whether to call it,
the state machine still decides what follows, and OPENED still comes only from the provider's
signed callback.

Rules of this contract:

* The bank asks for the newest version it supports. It moves to an older one ONLY when two things
  agree: the provider's answer names that version as a permitted fallback, and the bank's
  `COMPATIBILITY_POLICY` allows the step. "This version is gone" alone is not a permission.
* The provider did not process a request it answered with `ProviderVersionUnavailable` (like HTTP
  410), so asking again in an older version cannot create a second hand-over.
* A malformed, unexpected or wrong-version answer is NEVER retried in another version, and neither
  is a timeout or a lost connection: the provider may have acted. It becomes a refused submission,
  which the onboarding service audits, leaving the application where it was.
* An answer can only say accepted or not. Fields it does not define, such as ``opened``, make it
  invalid, because no synchronous answer may open an account (ADR section 05).
* A fallback changes the wire format only. Consent, the mandatory checks and the policy gate all
  run before the adapter is called, and the signed confirmation callback is not versioned.

The provider's signed manifest does not list API versions: its digest is pinned and unchanged. In
production the supported versions and their sunset dates belong in the signed manifest.
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
# The only moves the bank accepts, and only if the provider also names them.
COMPATIBILITY_POLICY: dict[str, tuple[str, ...]] = {"v2": ("v1",)}
NO_SUBMISSION_REF = "NO-SUBMISSION"

_logger = get_logger(__name__)


class ProviderVersionUnavailable(Exception):
    """The provider states that it no longer serves this version; the request was not processed.

    `fallback_to` names the versions the provider explicitly allows instead. Empty means none.
    """

    def __init__(self, version: str, *, fallback_to: tuple[str, ...] = ()) -> None:
        super().__init__(version)
        self.fallback_to = fallback_to


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
            return ProviderSubmission(
                provider_ref=v1.provider_ref,
                accepted=v1.accepted,
                mock=True,
                contract_version="v1",
            )
        if version == "v2":
            v2 = SubmissionResponseV2.model_validate(payload)
            return ProviderSubmission(
                provider_ref=v2.submission.reference,
                accepted=v2.submission.status == "ACCEPTED",
                mock=True,
                contract_version="v2",
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
        fallback_from: str | None = None
        for position, version in enumerate(self._versions):
            try:
                answer = self._transport(version, build_submission_request(version, application))
            except ProviderVersionUnavailable as gone:
                self._event(app_id, version, "version_unavailable")
                if not self._fallback_permitted(position, gone.fallback_to):
                    break
                fallback_from = version
                continue
            except (TimeoutError, ConnectionError):
                self._event(app_id, version, "provider_unavailable")
                return self._refused("provider_unavailable", version)  # it may have acted
            try:
                submission = parse_submission_response(version, answer)
            except ProviderContractViolation:
                self._event(app_id, version, "contract_violation")
                return self._refused("contract_violation", version)  # it may have acted
            self._event(app_id, version, "answered")
            return submission.model_copy(update={"fallback_from": fallback_from})
        self._event(app_id, "-", "no_supported_version")
        return self._refused("no_supported_version", None)

    def _fallback_permitted(self, position: int, offered: tuple[str, ...]) -> bool:
        following = self._versions[position + 1 : position + 2]
        if not following:
            return False
        current = self._versions[position]
        return following[0] in offered and following[0] in COMPATIBILITY_POLICY.get(current, ())

    def _event(self, application_id: str, version: str, event: str) -> None:
        if event != "answered":
            _logger.warning("depot contract: %s (%s) for %s", event, version, application_id)
        self._on_event(application_id, version, event)

    @staticmethod
    def _refused(reason: str, version: str | None) -> ProviderSubmission:
        return ProviderSubmission(
            provider_ref=NO_SUBMISSION_REF,
            accepted=False,
            mock=True,
            contract_version=version,
            refusal=reason,
        )


class MockVersionedDepotApi:
    """The MOCK provider's side of the contract: synthetic, deterministic, in-process.

    Asked for a version it does not serve, it answers that the version is gone and, if
    `permit_fallback`, names the older versions it still serves.
    """

    IS_MOCK: ClassVar[bool] = True

    def __init__(self, served: tuple[str, ...] = ("v1", "v2"), *, permit_fallback: bool = True):
        self.served = served
        self.permit_fallback = permit_fallback
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, version: ContractVersion, request: dict) -> Mapping[str, Any]:
        if version not in self.served:
            older = tuple(v for v in self.served if v in SUPPORTED_VERSIONS and v < version)
            raise ProviderVersionUnavailable(
                version, fallback_to=older if self.permit_fallback else ()
            )
        self.calls.append((version, request))
        if version == "v1":
            reference = f"MOCK-DEPOT-{request['application_id']}"
            return {"contract_version": "v1", "provider_ref": reference, "accepted": True}
        reference = f"MOCK-DEPOT-{request['application']['application_id']}"
        return {
            "contract_version": "v2",
            "submission": {"reference": reference, "status": "ACCEPTED"},
        }


def _malformed(version: ContractVersion, _request: dict) -> Mapping[str, Any]:
    return {"contract_version": version, "result": "unexpected shape"}


def _unavailable(_version: ContractVersion, _request: dict) -> Mapping[str, Any]:
    raise ConnectionError("synthetic outage")


def _times_out(_version: ContractVersion, _request: dict) -> Mapping[str, Any]:
    raise TimeoutError("synthetic timeout")


def _manipulated(version: ContractVersion, request: dict) -> Mapping[str, Any]:
    """A provider answer that tries to open the account synchronously."""
    answer = dict(MockVersionedDepotApi()(version, request))
    return {**answer, "opened": True, "state": "OPENED"}


# name -> (the versions the bank is configured to use, the provider's behaviour)
SCENARIOS: dict[str, Callable[[], tuple[tuple[str, ...], DepotTransport]]] = {
    "current_v2": lambda: (SUPPORTED_VERSIONS, MockVersionedDepotApi()),
    "v1_only": lambda: (("v1",), MockVersionedDepotApi(served=("v1",))),
    "v2_deprecated_fallback": lambda: (SUPPORTED_VERSIONS, MockVersionedDepotApi(served=("v1",))),
    "v2_retired_no_fallback": lambda: (
        SUPPORTED_VERSIONS,
        MockVersionedDepotApi(served=("v1",), permit_fallback=False),
    ),
    "unsupported_version": lambda: (SUPPORTED_VERSIONS, MockVersionedDepotApi(served=("v3",))),
    "malformed_response": lambda: (SUPPORTED_VERSIONS, _malformed),
    "provider_unavailable": lambda: (SUPPORTED_VERSIONS, _unavailable),
    "provider_timeout": lambda: (SUPPORTED_VERSIONS, _times_out),
    "manipulated_opened": lambda: (SUPPORTED_VERSIONS, _manipulated),
}


def build_mock_depot(scenario: str = "current_v2") -> VersionedDepotAdapter:
    """The adapter for a named scenario. An unknown name raises, so the application cannot start."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown mock depot scenario: {scenario}")
    versions, transport = SCENARIOS[scenario]()
    return VersionedDepotAdapter(transport, versions=versions)
