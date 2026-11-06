"""Observed v1/v2 provider behaviour through the real bank, as a compact Markdown table.

    python -m scripts.provider_compat_matrix
    python -m scripts.provider_compat_matrix --write docs/evidence/provider-compatibility-matrix.md

Each row runs the whole integrated journey in-process against the MOCK provider in that scenario:
the customer supplies the information and confirms, the agent submits, then a correctly signed
provider confirmation arrives. Every cell is read back from the application, not assumed. Synthetic
and offline. It shows how the bank behaves against a changing synthetic API, not a real provider.
"""

import argparse
import contextlib
import json
import logging
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.container import build_default_container
from app.core.config import Environment, Settings
from app.main import create_app
from scripts import customer_steps as steps
from tests.flow import PROVIDER_SECRET, FakeClock, Flow, Started

HEADER = (
    "| ID | Provider behaviour | Scenario setting | Version used | Fallback from | Submit answer "
    "| Adapter refusal | State after submit | Signed callback | Final state |"
)
SEPARATOR = "|---|---|---|---|---|---|---|---|---|---|"


@dataclass(frozen=True)
class Row:
    case_id: str
    behaviour: str
    scenario: str
    version: str
    fallback_from: str
    submit: int
    refusal: str
    state_after_submit: str
    callback: int
    final_state: str

    def markdown(self) -> str:
        return (
            f"| {self.case_id} | {self.behaviour} | `{self.scenario}` | {self.version} "
            f"| {self.fallback_from} | {self.submit} | {self.refusal} | {self.state_after_submit} "
            f"| {self.callback} | {self.final_state} |"
        )


CASES = (
    ("A", "Provider supports v1 only; the bank is configured for v1", "v1_only"),
    ("B", "Provider supports v2 and v1; the bank uses v2", "current_v2"),
    ("C", "Provider retired v2 and explicitly permits v1", "v2_deprecated_fallback"),
    ("C2", "Provider retired v2 and does not permit a fallback", "v2_retired_no_fallback"),
    ("D", "Provider offers only an unsupported version (v3)", "unsupported_version"),
    ("E", "Malformed response", "malformed_response"),
    ("F1", "Provider unavailable (connection lost)", "provider_unavailable"),
    ("F2", "Provider times out", "provider_timeout"),
    ("G", "Manipulated response claims the account is OPENED", "manipulated_opened"),
)


@contextlib.contextmanager
def _flow(scenario: str) -> Iterator[Flow]:
    workdir = Path(tempfile.mkdtemp(prefix="trusteq_matrix_"))
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        database_url=f"sqlite:///{(workdir / 'matrix.db').as_posix()}",
        mock_provider_callback_secret=SecretStr(PROVIDER_SECRET),
        customer_information_required=True,
        mock_depot_scenario=scenario,
        log_level="ERROR",
    )
    container = build_default_container(settings)
    clock = FakeClock()
    container.clock = clock
    app = create_app(settings, container)
    try:
        with TestClient(app):
            yield Flow(
                app=app,
                agent_http=TestClient(app),
                customer_http=TestClient(app),
                provider_http=TestClient(app),
                clock=clock,
                container=container,
                engine_getter=lambda: app.state.engine,
            )
    finally:
        app.state.engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)


def _submission_details(flow: Flow, application_id: str) -> dict[str, str]:
    rows = flow.sql(
        "SELECT details FROM audit_events WHERE application_id = :a AND "
        "event_type IN ('application.submitted', 'application.submit_denied') ORDER BY rowid",
        a=application_id,
    )
    details = rows[-1][0]
    return json.loads(details) if isinstance(details, str) else details


def _to_customer_confirmation(flow: Flow) -> Started:
    started = flow.start()
    link = steps.link_of(started.handoff_id, started.nonce)
    steps.sign_in(flow.customer_http, link, "cust-1")
    steps.complete_customer_information(flow.customer_http, link)
    if steps.post_confirmation(flow.customer_http, link).status_code != 200:
        raise RuntimeError("the customer's confirmation failed")
    return started


def observe(case_id: str, behaviour: str, scenario: str) -> Row:
    with _flow(scenario) as flow:
        started = _to_customer_confirmation(flow)
        app_id = started.application_id
        submit = flow.agent_http.post(
            f"/v1/applications/{app_id}/submit", headers=flow.onboarding_headers(app_id)
        )
        details = _submission_details(flow, app_id)
        after_submit = flow.status_of(app_id)
        callback = flow.provider_confirms(app_id)
        return Row(
            case_id=case_id,
            behaviour=behaviour,
            scenario=scenario,
            version=details.get("contract_version", "-"),
            fallback_from=details.get("fallback_from", "-"),
            submit=submit.status_code,
            refusal=details.get("refusal", "-"),
            state_after_submit=after_submit,
            callback=callback.status_code,
            final_state=flow.status_of(app_id),
        )


def observe_all(cases: tuple[tuple[str, str, str], ...] = CASES) -> list[Row]:
    return [observe(*case) for case in cases]


def render(rows: list[Row]) -> str:
    lines = [
        "# Provider v1/v2 compatibility matrix (observed, synthetic)",
        "",
        "Generated by `python -m scripts.provider_compat_matrix --write "
        "docs/evidence/provider-compatibility-matrix.md`. Do not edit it by hand: "
        "`tests/test_provider_integration.py` regenerates it and compares.",
        "",
        "Every row is the whole integrated journey through the real bank against the MOCK "
        "provider, with the customer's information and confirmation done first. The signed "
        "callback is a correctly signed provider confirmation sent after the submission.",
        "",
        HEADER,
        SEPARATOR,
        *[row.markdown() for row in rows],
        "",
        "**H, no unauthorized transition:** in every row whose submission failed, the application "
        "stays in `CONTRACT_PENDING`, no depot submission is recorded and the correctly signed "
        "callback is refused with 403. A later retry can still continue. See "
        "`tests/test_provider_integration.py`.",
        "",
        "**Limits:** the provider is an in-process mock, so a timeout is a raised error, not a "
        "real wait. A real timeout leaves the outcome unknown and needs reconciliation with the "
        "provider, which is not implemented. The signed manifest does not list API versions; "
        "its digest is pinned and unchanged. The signed callback is not versioned.",
        "",
    ]
    return "\n".join(lines)


def main(writer: Callable[[str], object] = print) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", type=Path, help="also write the table to this file")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.ERROR)  # client logs would show the one-time link
    text = render(observe_all())
    writer(text)
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
