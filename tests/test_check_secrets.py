"""The secret tripwire must catch the shapes this project actually uses."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_secrets


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text",
    [
        # Every string below is a fabricated example, which is the point of the test.
        "ANTHROPIC_API_KEY=sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # pragma: allowlist secret
        '"private_key": "-----BEGIN PRIVATE KEY-----',  # pragma: allowlist secret
        "-----BEGIN PRIVATE KEY-----",  # pragma: allowlist secret
        "key = AIza" + "B" * 35,  # a Google API key is AIza plus 35 characters
        "AKIAIOSFODNN7EXAMPLE",  # pragma: allowlist secret
        'password = "hunter2hunter2hunter2hunter2hunter2"',  # pragma: allowlist secret
    ],
)
def test_credential_shapes_are_caught(tmp_path, text):
    path = write(tmp_path, "leak.py", text)
    assert check_secrets.scan_file(path), f"missed: {text[:40]}"


@pytest.mark.parametrize(
    "text",
    [
        "ANTHROPIC_API_KEY=",
        "# Paste your key into .env as ANTHROPIC_API_KEY",
        "SHEET_ID=1a2b3c",
        "sheet_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
    ],
)
def test_ordinary_config_lines_are_not_flagged(tmp_path, text):
    path = write(tmp_path, "fine.py", text)
    assert check_secrets.scan_file(path) == []


def test_the_allowlist_marker_silences_a_deliberate_example(tmp_path):
    path = write(
        tmp_path,
        "doc.md",
        # Built from parts so this line does not itself look like a leak.
        "ANTHROPIC_API_KEY=sk-ant-" + "api03-" + "a" * 30 + f"  # {check_secrets.ALLOW_MARKER}",
    )
    assert check_secrets.scan_file(path) == []


def test_forbidden_filenames_are_caught_regardless_of_contents():
    findings = check_secrets.check_forbidden(
        [Path(".env"), Path("service_account.json"), Path("deploy.pem")]
    )
    assert len(findings) == 3


def test_the_env_example_is_allowed():
    assert check_secrets.check_forbidden([Path(".env.example")]) == []


def test_the_repository_itself_is_clean():
    """The real check, run against the real tree."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/check_secrets.py"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
