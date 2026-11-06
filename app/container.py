"""Composition root: which implementations back the ports.

Wires the verified local catalog fixture and the MOCK regulated providers. The depot provider goes
through the versioned contract (`app.integrations.provider_contract`). Tests (and later phases)
pass a different `AppContainer` to `create_app` to change an outcome, e.g.
`MockComplianceProvider(CheckOutcome.REVIEW_REQUIRED)`, or to control the clock.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings
from app.integrations.catalog_fixture import FixtureProviderDiscovery
from app.integrations.mock import (
    MockComplianceProvider,
    MockContractSigner,
    MockKycProvider,
)
from app.integrations.ports import (
    CompliancePort,
    ContractSigningPort,
    DepotProviderPort,
    KycPort,
    ProviderDiscoveryPort,
)
from app.integrations.provider_contract import build_mock_depot


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class AppContainer:
    catalog: ProviderDiscoveryPort
    kyc: KycPort
    compliance: CompliancePort
    contract: ContractSigningPort
    depot: DepotProviderPort
    clock: Callable[[], datetime] = field(default=_utc_now)


def build_default_container(settings: Settings) -> AppContainer:
    """Verified fixture catalog + MOCK providers returning PASS. Raises if the fixture is bad."""
    return AppContainer(
        catalog=FixtureProviderDiscovery(),
        kyc=MockKycProvider(),
        compliance=MockComplianceProvider(),
        contract=MockContractSigner(),
        depot=build_mock_depot(settings.mock_depot_scenario),
    )
