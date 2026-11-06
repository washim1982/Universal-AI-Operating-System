"""From candidates to answers: hybrid ranking and context packs (FR-MEM-03).

This runs above the store, so every backend ranks the same way (FR-MEM-02). A store only has to
find keyword and vector candidates; it never decides what wins.

* **Fusion.** Reciprocal rank fusion combines the two lists by position, because keyword and vector
  scores aren't comparable. Recency and importance then adjust the result.
* **Deduplication.** Near-identical items are dropped, so that a pack doesn't spend its budget
  repeating itself. An ingested document is often chunked with overlap.
* **Budget.** Whole items are added best first; the first that doesn't fit is cut at a sentence if
  enough of it survives. Tokens are estimated from characters, which is why the budget holds a
  margin: this runs on every recall, across every model, and needs no tokenizer.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aios.core.envelope import DataClass
from aios.core.memory import Citation, ContextPack, MemoryHit, MemoryItem, SearchMode

RRF_K = 60
KEYWORD_WEIGHT = 0.4
VECTOR_WEIGHT = 0.4
RECENCY_WEIGHT = 0.02
IMPORTANCE_WEIGHT = 0.02
"""Small on purpose. One rank near the top is worth about 0.013, so these reorder neighbours and
can't bury a good match under fresher, weaker ones."""
RECENCY_HALF_LIFE_DAYS = 30.0
DUPLICATE_THRESHOLD = 0.85
SHINGLE_WORDS = 3
CHARS_PER_TOKEN = 4
BUDGET_MARGIN = 0.9
MIN_TOKENS_TO_CUT = 40

_WORD = re.compile(r"\w+")
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _reciprocal(rank: int) -> float:
    """1.0 for the first candidate, falling slowly: the shape that makes two lists comparable."""
    return (RRF_K + 1) / (RRF_K + 1 + rank)


def fuse(
    keyword: Sequence[MemoryHit], vector: Sequence[MemoryHit], *, now: datetime
) -> list[MemoryHit]:
    """Keyword and vector candidates as one list, best first."""
    items: dict[str, MemoryItem] = {}
    relevance: dict[str, float] = {}
    for weight, candidates in ((KEYWORD_WEIGHT, keyword), (VECTOR_WEIGHT, vector)):
        for rank, hit in enumerate(candidates):
            items[hit.item.id] = hit.item
            relevance[hit.item.id] = relevance.get(hit.item.id, 0.0) + weight * _reciprocal(rank)
    scored = [
        MemoryHit(item=item, score=round(relevance[key] + _adjustment(item, now), 6))
        for key, item in items.items()
    ]
    return sorted(scored, key=lambda hit: (-hit.score, hit.item.id))


def _adjustment(item: MemoryItem, now: datetime) -> float:
    age_days = max((now - item.provenance.created_at).total_seconds(), 0.0) / 86_400
    recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    return RECENCY_WEIGHT * recency + IMPORTANCE_WEIGHT * item.importance


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower())
    if len(words) < SHINGLE_WORDS:
        return {tuple(words)}
    return {tuple(words[i : i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def deduplicated(hits: Sequence[MemoryHit]) -> list[MemoryHit]:
    """``hits`` without items that nearly repeat a better one."""
    kept: list[tuple[MemoryHit, set[tuple[str, ...]]]] = []
    for hit in hits:
        shingles = _shingles(hit.item.content)
        if all(_overlap(shingles, other) < DUPLICATE_THRESHOLD for _, other in kept):
            kept.append((hit, shingles))
    return [hit for hit, _ in kept]


def _overlap(left: set[tuple[str, ...]], right: set[tuple[str, ...]]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _escaped(text: str) -> str:
    """Retrieved text can't close its own wrapper and pose as instructions (FR-MEM-13)."""
    return re.sub(r"</?\s*memory", "(memory", text, flags=re.IGNORECASE)


def _attribute(value: str) -> str:
    """A URI comes from whoever ingested the document, so it can't end its attribute either."""
    return _escaped(value).replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _block(item: MemoryItem, text: str) -> str:
    uri = item.provenance.source_uri
    source = f' source="{_attribute(" ".join(uri.split()))}"' if uri else ""
    return f'<memory id="{item.id}" scope="{item.scope}"{source}>\n{_escaped(text)}\n</memory>'


def _cut(text: str, tokens: int) -> str | None:
    """``text`` cut to about ``tokens`` at a sentence end, or ``None`` if too little is left."""
    limit = tokens * CHARS_PER_TOKEN
    ends = [match.end() for match in _SENTENCE_END.finditer(text, 0, limit)]
    return text[: ends[-1]].rstrip() if ends else None


def assemble(hits: Sequence[MemoryHit], *, budget_tokens: int, mode: SearchMode) -> ContextPack:
    """The best deduplicated items that fit the budget, each one cited."""
    remaining = int(budget_tokens * BUDGET_MARGIN)
    blocks: list[str] = []
    citations: list[Citation] = []
    labels: list[DataClass] = []
    for hit in deduplicated(hits):
        block = _block(hit.item, hit.item.content)
        needed = estimate_tokens(block)
        if needed > remaining:
            wrapper = estimate_tokens(_block(hit.item, ""))
            shortened = _cut(hit.item.content, remaining - wrapper)
            if shortened is None or remaining - wrapper < MIN_TOKENS_TO_CUT:
                break
            block = _block(hit.item, shortened)
            needed = estimate_tokens(block)
        blocks.append(block)
        remaining -= needed
        labels.append(hit.item.sensitivity)
        citations.append(
            Citation(
                id=hit.item.id,
                scope=hit.item.scope,
                source_uri=hit.item.provenance.source_uri,
                score=hit.score,
            )
        )
    text = "\n\n".join(blocks)
    return ContextPack(
        text=text,
        citations=citations,
        tokens=estimate_tokens(text),
        sensitivity=DataClass.strictest(DataClass.PUBLIC, *labels),
        mode=mode,
    )


@dataclass(frozen=True, slots=True)
class Chunk:
    text: str
    index: int
    heading: str | None = None


_HEADING = re.compile(r"^#{1,6}\s+(.+)$")


def chunked(text: str, *, max_tokens: int = 300, overlap_tokens: int = 30) -> list[Chunk]:
    """Split a document on paragraphs, keeping each chunk's heading and a little overlap.

    A chunk starts with its heading so that it still makes sense when retrieved alone. A paragraph
    longer than a chunk is split at sentences, then at words.
    """
    chunks: list[Chunk] = []
    heading: str | None = None
    buffer: list[str] = []
    fresh = False  # the buffer holds more than the overlap carried over from the last chunk

    def flush() -> None:
        nonlocal fresh
        if not fresh:
            buffer.clear()
            return
        body = "\n\n".join(buffer)
        title = f"{heading}\n\n" if heading is not None and not body.startswith(heading) else ""
        chunks.append(Chunk(f"{title}{body}", len(chunks), heading))
        tail = body[-overlap_tokens * CHARS_PER_TOKEN :] if overlap_tokens else ""
        start = tail.find(" ")
        buffer.clear()
        fresh = False
        if start != -1 and len(body) > overlap_tokens * CHARS_PER_TOKEN:
            buffer.append(tail[start + 1 :])

    for paragraph in re.split(r"\n\s*\n", text):
        stripped = paragraph.strip()
        if not stripped:
            continue
        match = _HEADING.match(stripped.splitlines()[0])
        if match:
            flush()
            buffer.clear()
            heading = match.group(1).strip()
            rest = "\n".join(stripped.splitlines()[1:]).strip()
            if not rest:
                continue
            stripped = rest
        for piece in _pieces(stripped, max_tokens):
            if estimate_tokens("\n\n".join([*buffer, piece])) > max_tokens:
                flush()
                if estimate_tokens("\n\n".join([*buffer, piece])) > max_tokens:
                    buffer.clear()  # no room for the overlap beside a full-sized piece
            buffer.append(piece)
            fresh = True
    flush()
    return chunks


def _pieces(paragraph: str, max_tokens: int) -> list[str]:
    if estimate_tokens(paragraph) <= max_tokens:
        return [paragraph]
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        for word_group in _words(sentence, max_tokens):
            joined = f"{current} {word_group}".strip()
            if estimate_tokens(joined) > max_tokens and current:
                pieces.append(current)
                current = word_group
            else:
                current = joined
    if current:
        pieces.append(current)
    return pieces


def _words(sentence: str, max_tokens: int) -> list[str]:
    if estimate_tokens(sentence) <= max_tokens:
        return [sentence]
    groups: list[str] = []
    current = ""
    for word in sentence.split():
        joined = f"{current} {word}".strip()
        if estimate_tokens(joined) > max_tokens and current:
            groups.append(current)
            current = word
        else:
            current = joined
    if current:
        groups.append(current)
    return groups
