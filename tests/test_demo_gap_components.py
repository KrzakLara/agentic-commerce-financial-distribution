"""`python -m scripts.demo_gap_components` prints the outcomes that the docs point at."""

import pytest

from scripts import demo_gap_components


def test_the_demo_shows_each_outcome_and_no_identifier(capsys: pytest.CaptureFixture[str]) -> None:
    assert demo_gap_components.main() == 0
    out = capsys.readouterr().out
    for line in (
        "v2:answered",
        "v2:version_unavailable, v1:answered",
        "no_supported_version",
        "ASSESSMENT_OK",
        "WARNING_REQUIRED",
        "INSUFFICIENT_INFORMATION",
        "NOT_TRIGGERED_BY_ACCOUNT_OPENING",
        "ASSESSMENT_REMAINS_REQUIRED",
        "real-looking identifier   complete=false id=INVALID",
        "supplied by a model       complete=false id=INVALID",
        "evidence=False",
        "mock claiming a qualified signature -> refused",
        "the approved order fee   -> APPROVED",
        "a fabricated fee         -> UNSUPPORTED",
        "a guarantee              -> ESCALATE_TO_HUMAN",
    ):
        assert line in out, line
    assert "SYNTH-TAX-000123" not in out and "12345678901" not in out
