"""Retrieval quality on golden datasets (FR-MEM-12): recall@k and reciprocal rank.

Results are compared by document: a search that finds three chunks of one relevant document has
found one relevant document. A case names its relevant documents by name or by address, and a
name matches an address that ends with it, so ``returns-policy.md`` finds
``file:///kb/returns-policy.md``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from aios.core.memory import MemoryItem


def ranked_documents(items: Iterable[MemoryItem]) -> list[str]:
    """The documents found, best first, each once. An item from no document stands for itself."""
    found: list[str] = []
    for item in items:
        document = item.document or item.id
        if document not in found:
            found.append(document)
    return found


def _matches(name: str, document: str) -> bool:
    return document == name or document.endswith(f"/{name}")


def _relevant(relevant: Sequence[str], document: str) -> bool:
    return any(_matches(name, document) for name in relevant)


def recall_at(relevant: Sequence[str], documents: Sequence[str], k: int) -> float:
    """The share of the relevant documents among the first ``k`` found."""
    if not relevant:
        return 0.0
    top = documents[:k]
    hits = sum(any(_matches(name, doc) for doc in top) for name in relevant)
    return hits / len(relevant)


def reciprocal_rank(relevant: Sequence[str], documents: Sequence[str]) -> float:
    """1 over the rank of the first relevant document found; 0 if none was."""
    for rank, document in enumerate(documents, start=1):
        if _relevant(relevant, document):
            return 1 / rank
    return 0.0
