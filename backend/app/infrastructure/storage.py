"""Object storage port used by document ingestion."""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO, Protocol


class ObjectStorage(Protocol):
    """Minimal storage contract; local disk and S3 adapters implement this."""

    def put(self, key: str, source: BinaryIO) -> None:
        """Persist a stream under a stable, tenant-scoped key."""

    def open(self, key: str) -> BinaryIO:
        """Open a stored object for parsing."""

    def delete(self, key: str) -> None:
        """Delete an object during rollback or retention cleanup."""


class LocalObjectStorage:
    """Small development adapter; production storage belongs behind the port."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def check_ready(self) -> None:
        """Probe local read/write access without touching document objects."""
        marker = b"docpilot-storage-ready"
        # The unique probe stays inside the configured root and is removed on exit.
        with NamedTemporaryFile(mode="w+b", dir=self.root, prefix=".docpilot-ready-") as probe:
            probe.write(marker)
            probe.flush()
            probe.seek(0)
            if probe.read() != marker:
                raise OSError("Local storage readiness probe could not read its data")

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if self.root.resolve() not in candidate.parents:
            raise ValueError("storage key escapes configured root")
        return candidate

    def put(self, key: str, source: BinaryIO) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()
