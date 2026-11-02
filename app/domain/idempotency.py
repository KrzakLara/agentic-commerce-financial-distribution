from pydantic import AwareDatetime

from app.domain.base import DomainModel, OpaqueId, Sha256Hex, ShortText


class IdempotencyRecord(DomainModel):
    """Remembers the outcome of one idempotent operation for one actor.

    Scoped per (actor, operation, key): two actors can never share or probe each other's keys.
    `request_hash` is the SHA-256 of the canonical request body.
    """

    actor_id: OpaqueId
    operation: ShortText
    idempotency_key: OpaqueId
    request_hash: Sha256Hex
    application_id: OpaqueId
    created_at: AwareDatetime
