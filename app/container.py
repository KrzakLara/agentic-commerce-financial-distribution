"""Composition root: which implementations back the ports.

Wires the verified local catalog fixture and the MOCK regulated providers. The depot provider goes
through the versioned contract (`app.integrations.provider_contract`). Tests (and later phases)
pass a different `AppContainer` to `create_app` to change an outcome, e.g.
`MockComplianceProvider(CheckOutcome.REVIEW_REQUIRED)`, or to control the clock.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from joserfc.jwk import ECKey

from app.core.config import Settings
from app.integrations.catalog_fixture import FixtureProviderDiscovery
from app.integrations.jose_tools import generate_key, load_private_key
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
from app.integrations.safe_http import default_client


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
    # The provider's metadata signing key: proves integrity, not identity or authorization.
    provider_key: ECKey = field(default_factory=lambda: generate_key("provider-ephemeral"))
    # Outbound requests to configured providers (OIDC, signing, proofing). Tests swap the transport.
    outbound_http: Callable[[], httpx.Client] = field(default=default_client)
    # The model client of the bank's question answering, in LLM mode only. Empty until first used;
    # the offline evaluation and the tests put a fake one here, on the real adapter's path.
    answer_model_client: Any = None
    # Paid model runs left in this process, set up on first use from the configured limit.
    answer_budget: Any = None


def build_default_container(settings: Settings) -> AppContainer:
    """Verified fixture catalog + MOCK providers returning PASS. Raises if the fixture is bad."""
    configured_key = settings.provider_signing_jwk
    return AppContainer(
        catalog=FixtureProviderDiscovery(),
        kyc=MockKycProvider(),
        compliance=MockComplianceProvider(),
        contract=MockContractSigner(),
        depot=build_mock_depot(settings.mock_depot_scenario),
        provider_key=(
            load_private_key(configured_key.get_secret_value())
            if configured_key
            else generate_key("provider-ephemeral")
        ),
    )
