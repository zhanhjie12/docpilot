"""Keep evaluation fixtures inside the repository on restricted Windows hosts."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest


SCRATCH_ROOT = Path(__file__).resolve().parent / ".scratch"


@pytest.fixture
def tmp_path():
    """Provide a unique writable folder without relying on system TEMP ACLs."""

    SCRATCH_ROOT.mkdir(exist_ok=True)
    path = SCRATCH_ROOT / uuid4().hex
    path.mkdir()
    try:
        yield path
    finally:
        # Verify the resolved target stays below this dedicated test folder.
        if not path.resolve().is_relative_to(SCRATCH_ROOT.resolve()):
            raise RuntimeError("refusing to remove a path outside eval scratch")
        shutil.rmtree(path)
