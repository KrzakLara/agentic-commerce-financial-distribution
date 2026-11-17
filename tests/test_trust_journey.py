"""The integrated journey across real, separate processes, including the Authlib OIDC provider."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_journey_and_its_negative_cases_pass_across_separate_processes() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRUSTEQ_", "ANTHROPIC_"))}
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(  # noqa: S603 - our own interpreter and module
        [sys.executable, "-m", "scripts.demo_trust_journey"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    output = result.stdout
    assert "RESULT: PASS" in output
    for marker in (
        "provider_verified",
        "delegation_granted",
        "final_status",
        "untrusted_anchor",
        "revoked_delegation_refused",
        "MANUAL_REVIEW",
        "decided REJECT on the trusted console",
        "REJECTED: a person can end the case",
        "TEST_NO_LEGAL_EFFECT",
    ):
        assert marker in output, marker
    for secret in ("tqa_", "token=", "device_code"):
        assert secret not in output, secret
