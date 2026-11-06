"""`python -m scripts.demo_integrated`: the real server, offline, with each assessment profile."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.customer_steps import REAL_LOOKING_TAX_ID, SYNTHETIC_TAX_ID

ROOT = Path(__file__).resolve().parent.parent


def run_demo(*arguments: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRUSTEQ_", "ANTHROPIC_"))}
    return subprocess.run(  # noqa: S603 - fixed argument list
        [sys.executable, "-m", "scripts.demo_integrated", *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    ("profile", "outcome"),
    [
        ("experienced", "ASSESSMENT_OK"),
        ("novice", "WARNING_REQUIRED"),
        ("incomplete", "INSUFFICIENT_INFORMATION"),
    ],
)
def test_the_journey_reaches_opened_with_every_assessment_result(
    profile: str, outcome: str
) -> None:
    result = run_demo("--profile", profile)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "RESULT: PASS" in out and outcome in out
    assert "OPENED" in out
    for line in (
        "approved fact: According to the verified synthetic price list",
        "declined",
        "goes to a person",
        "locally validated, NOT externally verified",
        "contract_version",
        "signed callback -> HTTP 200",
        "no tax identifier in any table",
        "the sign-in is a DEV simulation",
    ):
        assert line in out, line
    assert SYNTHETIC_TAX_ID not in out and REAL_LOOKING_TAX_ID not in out
    assert not re.search(r"token=(?!\*\*\*)", out)
    assert not re.search(r"tqa_[A-Za-z0-9_\-]{20,}", out)
    assert not re.search(r"\b[A-Za-z0-9_\-]{43}\b", out)


def test_a_failing_provider_stops_the_demo_journey_safely() -> None:
    result = run_demo("--depot-scenario", "manipulated_opened")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no unauthorized transition happened" in result.stdout
    assert "contract_violation" in result.stdout
