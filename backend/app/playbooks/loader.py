"""Load only bundled, reviewed YAML rules; never evaluate YAML as code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .models import Playbook


RULES_DIR = Path(__file__).resolve().parent / "rules"


@lru_cache(maxsize=8)
def load_playbook(name: str = "contract_v1") -> Playbook:
    """Pin callers to a known file name inside the package."""

    if not name or not all(char.isascii() and (char.isalnum() or char == "_") for char in name):
        raise ValueError("invalid playbook name")
    path = RULES_DIR / f"{name}.yaml"
    if not path.is_file():
        raise ValueError("unknown playbook")
    return Playbook.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
