from typing import Self

from pydantic import AwareDatetime, model_validator

from app.domain.base import DomainModel, OpaqueId, Sha256Hex


class CustomerSession(DomainModel):
    """A signed-in customer on the bank-controlled pages (DEV-simulated sign-in, Phase 03).

    Server-side record: the browser holds a random session secret in an HttpOnly cookie; only
    its SHA-256 hash is stored here. Expiry and revocation are decided by the server, never by
    the cookie. It is a completely separate credential from an agent's bearer token: other
    table, other prefix, never accepted on agent routes (and vice versa).
    """

    session_id: OpaqueId  # public identifier for audit events; not a credential
    secret_hash: Sha256Hex
    customer_id: OpaqueId
    created_at: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _expiry_after_creation(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    def is_active(self, now: AwareDatetime) -> bool:
        return self.revoked_at is None and self.created_at <= now < self.expires_at
