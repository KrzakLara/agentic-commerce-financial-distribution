"""Framework-independent application exceptions.

Each exception carries an HTTP status and a stable machine-readable code. The API layer
(`app.api.error_handlers`) turns them into the consistent JSON error envelope. Messages
must be safe to show to an untrusted caller: no compliance detail, no internals.
"""


class AppError(Exception):
    status_code: int = 400
    code: str = "app_error"
    # Extra response headers (e.g. the RFC 6750 `WWW-Authenticate` challenge). Never secrets.
    headers: dict[str, str] | None = None

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.__class__.default_message()
        super().__init__(self.message)

    @classmethod
    def default_message(cls) -> str:
        return "The request could not be processed."


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"

    @classmethod
    def default_message(cls) -> str:
        return "The requested resource was not found."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"

    @classmethod
    def default_message(cls) -> str:
        return "The request conflicts with the current state."


class PolicyDeniedError(AppError):
    """A deterministic policy gate refused the action (fail closed)."""

    status_code = 403
    code = "policy_denied"

    @classmethod
    def default_message(cls) -> str:
        return "The action is not permitted."


class AuthenticationRequiredError(AppError):
    status_code = 401
    code = "authentication_required"

    @classmethod
    def default_message(cls) -> str:
        return "Authentication is required."


AGENT_BEARER_REALM = "trusteq-agent-api"


class AgentAuthenticationRequiredError(AuthenticationRequiredError):
    """No agent bearer token was presented (RFC 6750: challenge without an error code)."""

    headers = {"WWW-Authenticate": f'Bearer realm="{AGENT_BEARER_REALM}"'}


class InvalidTokenError(AuthenticationRequiredError):
    """The presented bearer token is unknown, malformed, expired, revoked, or was issued by
    another issuer or for another audience. Deliberately ONE answer for all of these: the
    precise reason is audited, never returned."""

    code = "invalid_token"
    headers = {"WWW-Authenticate": f'Bearer realm="{AGENT_BEARER_REALM}", error="invalid_token"'}

    @classmethod
    def default_message(cls) -> str:
        return "The access token is invalid, expired or revoked."


class InsufficientScopeError(PolicyDeniedError):
    """A valid token, but its scopes do not cover the requested operation."""

    code = "insufficient_scope"
    headers = {
        "WWW-Authenticate": f'Bearer realm="{AGENT_BEARER_REALM}", error="insufficient_scope"'
    }

    @classmethod
    def default_message(cls) -> str:
        return "The access token does not permit this operation."


class InvalidTransitionError(ConflictError):
    """The requested workflow transition is not an allowed edge of the state machine."""

    code = "invalid_transition"

    @classmethod
    def default_message(cls) -> str:
        return "The requested state change is not allowed."


class TransitionNotAuthorizedError(PolicyDeniedError):
    """The edge exists, but the caller's authority is not allowed to take it."""

    code = "transition_not_authorized"

    @classmethod
    def default_message(cls) -> str:
        return "The caller may not perform this state change."


class ConcurrentModificationError(ConflictError):
    """Another writer changed the record first (optimistic-lock failure); safe to retry."""

    code = "concurrent_modification"

    @classmethod
    def default_message(cls) -> str:
        return "The resource was modified concurrently. Retry the request."


class IdempotencyConflictError(ConflictError):
    """Same idempotency key reused with a different request body."""

    code = "idempotency_conflict"

    @classmethod
    def default_message(cls) -> str:
        return "The Idempotency-Key was already used with a different request."


class InvalidStateError(ConflictError):
    """The application is not in a state that allows the requested operation."""

    code = "invalid_state"

    @classmethod
    def default_message(cls) -> str:
        return "The application is not in a state that allows this operation."


class PrerequisitesNotMetError(ConflictError):
    """A mandatory prerequisite is missing. The reason is audited, never shown to the agent."""

    code = "prerequisites_not_met"

    @classmethod
    def default_message(cls) -> str:
        return "Customer authorization and required checks are not complete."


class MandateInactiveError(PolicyDeniedError):
    """The onboarding mandate is missing, expired, revoked or lacks the needed scope."""

    code = "mandate_inactive"

    @classmethod
    def default_message(cls) -> str:
        return "The onboarding mandate is not active for this operation."


class UnverifiedReferenceError(AppError):
    """Provider, product or channel is not part of the verified catalog."""

    status_code = 422
    code = "unverified_reference"

    @classmethod
    def default_message(cls) -> str:
        return "Provider, product or channel is not recognised."


class CatalogIntegrityError(AppError):
    """The catalog fixture failed its integrity check; fail closed."""

    status_code = 503
    code = "catalog_unavailable"

    @classmethod
    def default_message(cls) -> str:
        return "The provider catalog is currently unavailable."


class HandoffInvalidError(PolicyDeniedError):
    """Unknown handoff or wrong challenge. Deliberately indistinguishable."""

    code = "handoff_invalid"

    @classmethod
    def default_message(cls) -> str:
        return "The customer handoff is not valid."


class HandoffExpiredError(AppError):
    status_code = 410
    code = "handoff_expired"

    @classmethod
    def default_message(cls) -> str:
        return "The customer handoff has expired."


class HandoffNotActiveError(ConflictError):
    """Already used or superseded by a newer handoff."""

    code = "handoff_not_active"

    @classmethod
    def default_message(cls) -> str:
        return "The customer handoff was already used or replaced."


class ActionDigestMismatchError(ConflictError):
    code = "action_digest_mismatch"

    @classmethod
    def default_message(cls) -> str:
        return "The approved action does not match the requested action."


class CustomerMismatchError(PolicyDeniedError):
    code = "customer_mismatch"

    @classmethod
    def default_message(cls) -> str:
        return "The authenticated customer is not the customer of this application."


class ServiceDisabledError(AppError):
    status_code = 503
    code = "service_disabled"

    @classmethod
    def default_message(cls) -> str:
        return "This interface is not enabled."


class ProviderUnavailableError(AppError):
    """The (mock) depot provider did not accept the hand-over; nothing was advanced."""

    status_code = 502
    code = "provider_unavailable"

    @classmethod
    def default_message(cls) -> str:
        return "The depot provider is currently unavailable."


class ConfirmationRejectedError(PolicyDeniedError):
    """A provider confirmation failed identity or consistency checks."""

    code = "confirmation_rejected"

    @classmethod
    def default_message(cls) -> str:
        return "The confirmation was rejected."
