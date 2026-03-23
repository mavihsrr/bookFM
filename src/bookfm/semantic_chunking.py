from __future__ import annotations

import math
from dataclasses import replace


from google import genai

from .config import (
    DEFAULT_SECTION_MAX_CHARS,
    DEFAULT_SECTION_MAX_SECONDS,
    DEFAULT_SECTION_MIN_SECONDS,
    DEFAULT_SECTION_TARGET_SECONDS,
    SEMANTIC_CHUNK_MAX_BLOCKS,
)
from .models import Document, DocumentSection


def _cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return 0.0 if denom == 0.0 else dot / denom


def _stats(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, math.sqrt(var)


async def embed_blocks_google(blocks: list[str], api_key: str) -> list[list[float]]:
    client = genai.Client(api_key=api_key)
    response = await client.aio.models.embed_content(
        model="gemini-embedding-001",
        contents=blocks,
    )
    embeddings = response.embeddings or []
    vectors: list[list[float]] = []
    for emb in embeddings:
        vectors.append(list(emb.values or []))
    return vectors


async def semantic_breakpoints(
    blocks: list[str],
    *,
    api_key: str,
    backend: str = "google",
) -> set[int]:
    if len(blocks) < 3:
        return set()

    limited = blocks[:SEMANTIC_CHUNK_MAX_BLOCKS]
    if backend == "google":
        vectors = await embed_blocks_google(limited, api_key=api_key)
    else:
        raise ValueError(f"Unknown embedding backend: {backend}")
    if len(vectors) != len(limited):
        return set()

    sims = [_cosine(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)]
    mean, stdev = _stats(sims)
    cutoff = mean - stdev

    breaks: set[int] = set()
    for i, sim in enumerate(sims):
        if sim < cutoff:
            breaks.add(i)
    return breaks


async def apply_semantic_breaks(
    document: Document,
    api_key: str,
    *,
    reading_speed_wpm: int,
    blocks: list[str],
    base_sections: list[DocumentSection],
    embed_backend: str = "google",
    embed_model: str | None = None,
) -> Document:
    # If deterministic chunking already produced many sections, keep it.
    if len(base_sections) >= 4 or len(blocks) < 6:
        document.sections = base_sections
        return document

    _ = embed_model
    breaks = await semantic_breakpoints(blocks, api_key=api_key, backend=embed_backend)
    if not breaks:
        document.sections = base_sections
        return document

    # Rebuild sections using the same max-size constraints, with semantic breaks as additional (optional) cut points.
    rebuilt: list[DocumentSection] = []
    current: list[str] = []

    def estimate_seconds(words: int) -> int:
        return max(6, round(words / (max(reading_speed_wpm, 1) / 60.0)))

    target_seconds = DEFAULT_SECTION_TARGET_SECONDS
    min_seconds = DEFAULT_SECTION_MIN_SECONDS
    max_seconds = DEFAULT_SECTION_MAX_SECONDS
    max_chars = DEFAULT_SECTION_MAX_CHARS
    target_words = max(40, round((reading_speed_wpm / 60.0) * target_seconds))
    max_words = max(40, round((reading_speed_wpm / 60.0) * max_seconds))

    def flush() -> None:
        if not current:
            return
        text = "\n\n".join(current).strip()
        words = len(text.split())
        estimated_seconds = estimate_seconds(words)
        rebuilt.append(
            DocumentSection(
                index=len(rebuilt),
                title=f"Section {len(rebuilt) + 1}",
                text=text,
                word_count=words,
                estimated_seconds=estimated_seconds,
                paragraph_count=len(current),
            )
        )

    for i, block in enumerate(blocks):
        projected = ("\n\n".join([*current, block])).strip()
        projected_words = len(projected.split())
        projected_seconds = estimate_seconds(projected_words)

        current_text = "\n\n".join(current).strip()
        current_words = len(current_text.split()) if current else 0
        current_seconds = estimate_seconds(current_words) if current_words else 0

        semantic_break = (
            bool(current)
            and i in breaks
            and current_seconds >= min_seconds
            and current_words >= target_words
        )

        if current and (
            projected_words > max_words
            or projected_seconds > max_seconds
            or len(projected) > max_chars
            or semantic_break
        ):
            flush()
            current = [block]
            continue

        current.append(block)

    flush()

    # Keep contexts consistent with existing behavior.
    for idx, section in enumerate(rebuilt):
        before = rebuilt[idx - 1].text[:200] if idx > 0 else ""
        after = rebuilt[idx + 1].text[:200] if idx + 1 < len(rebuilt) else ""
        rebuilt[idx] = replace(section, context_before=before, context_after=after)

    document.sections = rebuilt
    return document
