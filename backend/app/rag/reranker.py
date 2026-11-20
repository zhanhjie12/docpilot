"""Deterministic reranking hook for the local lexical retriever."""

from app.rag.retriever import RetrievedChunk


def rerank(candidates: list[RetrievedChunk], question: str, top_k: int = 5) -> list[RetrievedChunk]:
    """Return candidates sorted by their lexical score.

    A cross-encoder can replace this function without changing the route or
    citation contract.
    """

    return sorted(candidates, key=lambda item: item.score, reverse=True)[: max(1, top_k)]
