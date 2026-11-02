"""Guards for repository hygiene: secrets, .claude/, local databases."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_IGNORE_ENTRIES = [
    ".env", ".venv/", "__pycache__/", ".pytest_cache/", ".ruff_cache/", "*.db", "*.sqlite",
    "*.sqlite3", "build/", "dist/", "*.egg-info/", "tmp/", ".claude/", ".claude/skills/",
]  # fmt: skip


def test_gitignore_excludes_required_paths() -> None:
    lines = {
        line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    missing = [entry for entry in REQUIRED_IGNORE_ENTRIES if entry not in lines]
    assert not missing, f".gitignore is missing: {missing}"
    assert "!.env.example" in lines


def test_no_project_local_claude_directory() -> None:
    assert not (ROOT / ".claude").exists()


def test_env_example_contains_only_placeholders() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^TRUSTEQ_LLM_API_KEY=$", text, flags=re.MULTILINE)
    assert not re.search(r"sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|BEGIN [A-Z ]*PRIVATE KEY", text)


def test_env_example_matches_settings_fields() -> None:
    from app.core.config import Settings

    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^TRUSTEQ_([A-Z_]+)=", text, flags=re.MULTILINE))
    assert documented == {name.upper() for name in Settings.model_fields}
