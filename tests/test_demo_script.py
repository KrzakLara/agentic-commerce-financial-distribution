"""`python -m scripts.demo_happy` really runs the whole journey over a live server."""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def demo_dirs() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("trusteq_demo_*")}


def run_demo(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [sys.executable, "-m", "scripts.demo_happy", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_demo_completes_the_whole_journey_and_is_repeatable() -> None:
    before = demo_dirs()
    outputs = []
    for _ in range(2):  # a second run proves it does not depend on leftovers from the first
        result = run_demo()
        assert result.returncode == 0, result.stdout[-1500:] + result.stderr[-800:]
        outputs.append(result.stdout)

    for out in outputs:
        assert "RESULT: PASS" in out
        assert out.count("== ") >= 8  # all demo stages were printed
        for milestone in (
            "discovered provider fixture-bank",
            "AWAITING_AUTHORIZATION -> AUTHORIZED by CUSTOMER_CHANNEL",
            "SUBMITTED_TO_DEPOT_PROVIDER -> OPENED by DEPOT_PROVIDER_CONFIRMATION",
            "[mandate revoked]",
            "after restart the server still reports: OPENED",
        ):
            assert milestone in out, milestone
    ids = [re.search(r"created application (app-[0-9a-f]{32})", o).group(1) for o in outputs]  # type: ignore[union-attr]
    assert ids[0] != ids[1]  # a clean dataset every run: new application each time
    assert demo_dirs() == before  # the temporary database was cleaned up


def test_demo_never_prints_credentials_or_the_one_time_challenge() -> None:
    result = run_demo("--no-restart")
    assert result.returncode == 0, result.stdout[-1500:]
    out = result.stdout + result.stderr
    assert "token=***" in out
    assert not re.search(r"token=(?!\*\*\*)", out), "the one-time challenge was printed"
    assert not re.search(r"\b[A-Za-z0-9_\-]{43}\b", out), "a nonce-shaped value was printed"
    assert not re.search(r"X-Provider-Signature|sha256=[0-9a-f]{16}", out)
    assert "secret" not in out.lower().replace("(mock)", "")


def test_demo_does_not_touch_project_data() -> None:
    project_db, env_file = ROOT / "trusteq.db", ROOT / ".env"
    existed, env_existed = project_db.exists(), env_file.exists()  # a local .env is allowed
    assert run_demo("--no-restart").returncode == 0
    assert project_db.exists() == existed  # it never created or used the project database
    assert env_file.exists() == env_existed  # and it never creates a .env
    assert not (ROOT / ".claude").exists()
