"""The tool boundary between the untrusted external agent (T0b) and the bank side (T1/T2).

This module is the ONLY contract the agent package may depend on. It defines the typed
tool schemas (ADR-09: enum-like names, opaque ids, bounded strings, extra fields rejected)
and the `AgentToolGateway` port. The bank-side implementation of the gateway (calling the
REST API or an equivalent validated client) is added in Phase 01/02.

There are intentionally NO tools for confirming a handoff, approving KYC, signing a contract,
marking a depot OPENED or reading compliance data. `request_handoff` and `submit_after_consent`
are *requests*: the backend decides (mandate, consent, policy) and performs any state change.
"""

from enum import StrEnum
from typing import Any, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.domain.base import OpaqueId


class AgentToolName(StrEnum):
    DISCOVER_PROVIDER = "discover_provider"
    GET_OFFICIAL_PRODUCT = "get_official_product"
    CREATE_DRAFT = "create_draft"
    REQUEST_HANDOFF = "request_handoff"
    GET_GENERIC_STATUS = "get_generic_status"
    SUBMIT_AFTER_CONSENT = "submit_after_consent"


class _ToolArguments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DiscoverProviderArgs(_ToolArguments):
    provider_id: OpaqueId


class GetOfficialProductArgs(_ToolArguments):
    provider_id: OpaqueId
    product_id: OpaqueId


class CreateDraftArgs(_ToolArguments):
    provider_id: OpaqueId
    product_id: OpaqueId
    channel_id: OpaqueId
    customer_ref: OpaqueId
    idempotency_key: UUID


class RequestHandoffArgs(_ToolArguments):
    application_id: OpaqueId


class GetGenericStatusArgs(_ToolArguments):
    application_id: OpaqueId


class SubmitAfterConsentArgs(_ToolArguments):
    application_id: OpaqueId


TOOL_ARGUMENT_MODELS: dict[AgentToolName, type[_ToolArguments]] = {
    AgentToolName.DISCOVER_PROVIDER: DiscoverProviderArgs,
    AgentToolName.GET_OFFICIAL_PRODUCT: GetOfficialProductArgs,
    AgentToolName.CREATE_DRAFT: CreateDraftArgs,
    AgentToolName.REQUEST_HANDOFF: RequestHandoffArgs,
    AgentToolName.GET_GENERIC_STATUS: GetGenericStatusArgs,
    AgentToolName.SUBMIT_AFTER_CONSENT: SubmitAfterConsentArgs,
}


class AgentToolCall(BaseModel):
    """A tool call proposed by the agent. Untrusted: the bank side re-validates everything."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: AgentToolName
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def _arguments_match_tool_schema(self) -> Self:
        TOOL_ARGUMENT_MODELS[self.tool].model_validate(self.arguments)
        return self


class AgentVisibleStatus(StrEnum):
    """Coarse status the agent may see. Never carries compliance rationale."""

    OK = "OK"
    IN_PROGRESS = "IN_PROGRESS"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    DENIED = "DENIED"
    CLOSED = "CLOSED"  # application ended (declined / cancelled / failed)
    UNAVAILABLE = "UNAVAILABLE"


class AgentToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: AgentToolName
    status: AgentVisibleStatus
    data: dict[str, Any] = {}


class AgentToolGateway(Protocol):
    """Port implemented on the trusted side; the agent holds nothing but this."""

    def execute(self, call: AgentToolCall) -> AgentToolResult: ...
