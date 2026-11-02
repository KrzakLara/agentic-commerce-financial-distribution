"""Shared building blocks for domain models."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

# Opaque, bounded identifier: no PII, no free text (ADR: "opaque application_id").
OpaqueId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
]
# Same alphabet as the X-Correlation-ID header accepted by the API middleware (dots allowed).
CorrelationId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._\-]+$")
]
# Lower-case hex SHA-256 digest (document hashes, immutable-action digests).
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]


class DomainModel(BaseModel):
    """Immutable, strict base: unknown fields are rejected, instances cannot be mutated."""

    model_config = ConfigDict(frozen=True, extra="forbid")
