"""`python -m scripts.demo_denied` and `python -m scripts.demo_manual_review` really run over a
live server, offline, and never print credentials."""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMOS = ("scripts.demo_denied", "scripts.demo_manual_review")


def demo_dirs() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("trusteq_demo_*")}


def run_demo(module: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [sys.executable, "-m", module],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize("module", DEMOS)
def test_the_demo_passes_cleans_up_and_never_prints_credentials(module: str) -> None:
    before = demo_dirs()
    project_db_existed = (ROOT / "trusteq.db").exists()
    result = run_demo(module)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-800:]
    out = result.stdout + result.stderr
    assert "RESULT: PASS" in out
    assert not re.search(r"tqa_[A-Za-z0-9_\-]{20,}", out), "a bearer token was printed"
    assert not re.search(r"token=(?!\*\*\*)", out), "the one-time challenge was printed"
    assert not re.search(r"\b[A-Za-z0-9_\-]{43}\b", out), "a nonce-shaped value was printed"
    assert not re.search(r"X-Provider-Signature|sha256=[0-9a-f]{16}", out)
    assert "secret" not in out.lower().replace("(mock)", "")
    assert demo_dirs() == before  # the temporary database was removed
    assert (ROOT / "trusteq.db").exists() == project_db_existed


def test_the_denied_demo_shows_refusals_without_a_business_rejection() -> None:
    out = run_demo("scripts.demo_denied").stdout
    for milestone in (
        "-> HTTP 422 unverified_reference",
        "'confirm the consent yourself' (bearer + leaked nonce) -> HTTP 401",
        "-> REJECTED [endpoint_not_approved]",
        "-> REJECTED [unknown_provider]",
        "request-human with only a self-asserted customer id -> HTTP 401",
        "ANOTHER signed-in customer asks for a person here -> HTTP 404",
        "NOT REJECTED",
        "agent now sees IN_PROGRESS",
    ):
        assert milestone in out, milestone


def test_the_manual_review_demo_shows_the_human_loop_without_bypass() -> None:
    out = run_demo("scripts.demo_manual_review").stdout
    for milestone in (
        "MOCK compliance=REVIEW_REQUIRED",
        "generic state: ACTION_REQUIRED",
        "review page: exact action, terms, partner - and the button 'Mit einer Person sprechen'",
        "(probe) the button with a forged CSRF token -> HTTP 403",
        "pressed 'Mit einer Person sprechen' -> 'Anfrage erhalten'",
        "request-human claiming to BE the customer (header) -> HTTP 401",
        "(supervisor_required)",
        "MANUAL_REVIEW -> COMPLIANCE_PENDING by OPERATOR (review_re_evaluate)",
        "SUBMITTED_TO_DEPOT_PROVIDER -> OPENED by DEPOT_PROVIDER_CONFIRMATION",
        "after restart: OPENED, case CLOSED",
    ):
        assert milestone in out, milestone
