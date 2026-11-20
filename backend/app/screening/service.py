"""Application service for screening a document set."""

from collections.abc import Mapping, Sequence

from .filter_engine import screen_metadata
from .models import ScreeningConditions, ScreeningMatch


class ScreeningService:
    """Run safe filters first; deeper LLM analysis happens after this boundary."""

    def screen(
        self,
        documents: Sequence[Mapping[str, object]],
        conditions: ScreeningConditions,
    ) -> list[ScreeningMatch]:
        return screen_metadata(documents, conditions)

