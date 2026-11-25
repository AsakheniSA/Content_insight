"""Shared fixtures. No test in this suite touches the network."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """Stop a developer's real .env or shell from changing what the tests see."""
    for name in (
        "ANTHROPIC_API_KEY",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "SHEET_ID",
        "YOUTUBE_API_KEY",
        "TULA_TODAY",
        "TULA_LOG_LEVEL",
        "TULA_CONFIG_DIR",
        "GITHUB_ACTIONS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def real_config_dir() -> Path:
    """The repository's own config/ directory."""
    return ROOT / "config"


@pytest.fixture
def config_dir(tmp_path, real_config_dir, monkeypatch) -> Path:
    """A writable copy of the real config, so a test can corrupt one file safely."""
    destination = tmp_path / "config"
    shutil.copytree(real_config_dir, destination)
    monkeypatch.setenv("TULA_CONFIG_DIR", str(destination))
    return destination
