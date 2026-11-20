"""Composition-layer factory for rebuilding durable task runners.

Task rows store only a task type and JSON payload. Python callables cannot be
persisted safely, so startup recovery asks this factory to recreate a runner
from the same dependencies used by normal API requests.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.infrastructure.database import SQLiteDatabase
from app.infrastructure.providers import EmbeddingProvider, LLMProvider
from app.infrastructure.storage import ObjectStorage
from app.infrastructure.tasks import Runner
from app.services.document_ingestion import DocumentIngestionService
from app.services.review_service import review_runner
from app.services.screening_service import screen_runner


class TaskRunnerFactory:
    """Build supported runners from a persisted task type and payload."""

    def __init__(
        self,
        db: SQLiteDatabase,
        storage: ObjectStorage,
        settings: Settings,
        embedding_provider: EmbeddingProvider | None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self.db = db
        self.storage = storage
        self.settings = settings
        self.embedding_provider = embedding_provider
        self.llm_provider = llm_provider

    def resolve(self, task_type: str, payload: dict[str, Any]) -> Runner | None:
        """Return a fresh runner, or ``None`` when the task type is unsupported."""

        if task_type == "parse_and_index":
            service = DocumentIngestionService(
                self.db,
                self.storage,
                self.settings,
                self.embedding_provider,
            )
            return service.parse_version
        if task_type == "review":
            return review_runner(self.db, payload, self.embedding_provider, self.llm_provider)
        if task_type == "screen":
            return screen_runner(self.db, payload, self.embedding_provider)
        return None
