#!/usr/bin/env python3
"""Fail if anything that looks like a credential has been committed.

The repository is public, so a leaked key is a leaked key the moment it is pushed.
This runs in CI on every push and pull request, and is cheap enough to run locally
before committing:

    python scripts/check_secrets.py            # scan tracked files
    python scripts/check_secrets.py path ...   # scan specific files

It is a tripwire, not a guarantee. It looks for the shapes of credentials this project
actually uses, plus a few common ones, and for files that should never be tracked at
all. A finding is a stop-and-think, not necessarily a real leak — see FALSE POSITIVES
at the bottom of the output for how to annotate a deliberate example.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Marker that tells this script a line is a deliberate example, not a real secret.
ALLOW_MARKER = "pragma: allowlist secret"

#: (name, pattern) pairs. Keep patterns specific: a noisy check gets ignored.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("PEM private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "service-account private key field",
        re.compile(r'"private_key"\s*:\s*"-----BEGIN'),
    ),
    (
        "GitHub token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ),
    (
        "assigned secret value",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|passwd|token|credential)\b\s*[:=]\s*"
            r"['\"][A-Za-z0-9/+=_\-]{24,}['\"]"
        ),
    ),
)

#: Files that must never be tracked, whatever their contents.
FORBIDDEN_NAMES = (
    ".env",
    "service_account.json",
    "credentials.json",
)
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx")

#: Scanning these would be pointless or self-defeating.
SKIP_PATHS = {"scripts/check_secrets.py"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".woff",
                 ".woff2", ".ttf", ".lock"}
MAX_BYTES = 2_000_000


def tracked_files() -> list[Path]:
    """Every file git is tracking, which is exactly what a push would publish."""
    try:
        output = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Could not list tracked files: {error}", file=sys.stderr)
        return []
    return [Path(line) for line in output.splitlines() if line.strip()]


def should_scan(path: Path) -> bool:
    if path.as_posix() in SKIP_PATHS:
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if not path.is_file():
        return False
    return path.stat().st_size <= MAX_BYTES


def check_forbidden(paths: list[Path]) -> list[str]:
    findings = []
    for path in paths:
        name = path.name
        if name in FORBIDDEN_NAMES and name != ".env.example":
            findings.append(f"{path}: this file must never be committed")
        elif path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"{path}: key material must never be committed")
        elif path.suffix == ".json" and ("service-account" in name or "service_account" in name):
            findings.append(f"{path}: looks like a service-account key file")
    return findings


def scan_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(f"{path}:{number}: possible {label}")
                break
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for committed credentials.")
    parser.add_argument("paths", nargs="*", help="files to scan (default: all tracked files)")
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.paths] if args.paths else tracked_files()
    if not paths:
        print("Nothing to scan.")
        return 0

    findings = check_forbidden(paths)
    for path in paths:
        if should_scan(path):
            findings.extend(scan_file(path))

    if not findings:
        print(f"No credential-shaped strings found in {len(paths)} file(s).")
        return 0

    print("Possible credentials found — do not push until these are resolved:\n", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "\nIf a hit is a deliberate example, append this comment to the line:\n"
        f"  {ALLOW_MARKER}\n"
        "If it is real: rotate the credential first, then remove it from git history. "
        "Deleting it in a new commit is not enough — the old commit still holds it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
