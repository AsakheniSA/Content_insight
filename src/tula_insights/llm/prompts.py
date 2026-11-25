"""Loading versioned prompts from `prompts/`.

Every prompt file begins with a `version: N` line. The version is written onto each
row the prompt produced, so a row can always be traced back to the rules that made it.
Change a prompt's wording, bump its version.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def stamp(self) -> str:
        """What goes in a `prompt_version` column."""
        return f"{self.name}:{self.version}"


def prompts_dir() -> Path:
    override = os.environ.get("TULA_PROMPTS_DIR", "").strip()
    return Path(override) if override else ROOT / "prompts"


def load_prompt(name: str) -> Prompt:
    """Read `prompts/<name>.md` and split off its version header."""
    path = prompts_dir() / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    first, _, rest = raw.partition("\n")
    if not first.lower().startswith("version:"):
        raise ValueError(
            f"{path.name} must start with a line like 'version: 1' so that rows can be "
            f"traced to the prompt that produced them."
        )
    version = first.split(":", 1)[1].strip()
    if not version:
        raise ValueError(f"{path.name} has an empty version header")
    return Prompt(name=name, version=version, text=rest.strip())
