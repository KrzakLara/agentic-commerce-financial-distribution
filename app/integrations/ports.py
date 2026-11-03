"""Ports for the trusted bank side. Used by T2 services, never by the external agent."""

from collections.abc import Sequence
from typing import Protocol

from app.domain.application import DepotApplication
from app.domain.audit import AuditEvent
from app.domain.catalog import Channel, Product, Provider
from app.domain.enums import ReviewEvidenceCategory
from app.domain.receipts import ConsentReceipt
from app.integrations.results import CheckResult, ProviderSubmission, SigningResult


class ProviderDiscoveryPort(Protocol):
    """Verified provider discovery.

    Phase 04: a deterministic local fixture whose manifests are checked against the bank's
    provider register, pinned digests, a trust-anchor signature, domain/endpoint binding,
    capabilities, channels, version and validity window (see `catalog_fixture`).
    Implementations raise `NotFoundError` for unknown providers and `CatalogIntegrityError`
    when verification fails. Time validity is re-checked at use (`catalog_policy`).
    """

    def list_providers(self) -> Sequence[Provider]: ...

    def get_provider(self, provider_id: str) -> Provider: ...

    def list_products(self, provider_id: str) -> Sequence[Product]: ...

    def list_channels(self, provider_id: str) -> Sequence[Channel]: ...

    def get_document_text(self, document_id: str, document_version: str) -> str: ...


class KycPort(Protocol):
    """Identity verification. `evidence` (Phase 04) is passed only on a re-evaluation that an
    operator authorized after human review: categories of recorded, permitted evidence."""

    def verify_identity(
        self, application: DepotApplication, evidence: Sequence[ReviewEvidenceCategory] = ()
    ) -> CheckResult: ...


class CompliancePort(Protocol):
    """AML/compliance assessment. Raw flags and rationale stay inside T2/T3."""

    def assess(
        self, application: DepotApplication, evidence: Sequence[ReviewEvidenceCategory] = ()
    ) -> CheckResult: ...


class ContractSigningPort(Protocol):
    def record_signature(self, consent: ConsentReceipt) -> SigningResult: ...


class DepotProviderPort(Protocol):
    """Submission to the licensed depot provider. Never returns an 'opened' result."""

    def submit_application(self, application: DepotApplication) -> ProviderSubmission: ...


class AuditSinkPort(Protocol):
    """Audit persistence. Implemented by a repository in Phase 01."""

    def record(self, event: AuditEvent) -> None: ...


__all__ = [
    "AuditSinkPort",
    "CompliancePort",
    "ContractSigningPort",
    "DepotProviderPort",
    "KycPort",
    "ProviderDiscoveryPort",
]
