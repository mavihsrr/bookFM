from __future__ import annotations

import asyncio
from pathlib import Path

from .analysis import analyze_section
from .chunking import chunk_document, split_blocks
from .config import DEFAULT_PREFETCH_COUNT, DEFAULT_READING_SPEED_WPM, DEFAULT_SECTION_MAX_CHARS
from .ingest import load_document
from .models import Document, DocumentSection, GeneratedClip, MusicPlan
from .music import generate_section_audio
from .semantic_chunking import apply_semantic_breaks


async def prepare_document(
    *,
    text: str | None = None,
    text_file: Path | None = None,
    epub_file: Path | None = None,
    reading_speed_wpm: int = DEFAULT_READING_SPEED_WPM,
    semantic: bool = True,
    api_key: str | None = None,
) -> Document:
    document = await load_document(text=text, text_file=text_file, epub_file=epub_file)
    base = chunk_document(document, reading_speed_wpm=reading_speed_wpm)
    if not semantic:
        return base

    # Resolve API key from env if not provided
    if not api_key:
        import os
        api_key = os.getenv("GEMINI_API_KEY") or ""

    blocks = split_blocks(document.full_text, max_chars=DEFAULT_SECTION_MAX_CHARS)
    return await apply_semantic_breaks(
        document,
        api_key,
        reading_speed_wpm=reading_speed_wpm,
        blocks=blocks,
        base_sections=base.sections,
    )


async def build_section_plans(
    document: Document,
    *,
    api_key: str,
    reading_speed_wpm: int,
    start_index: int = 0,
    count: int = DEFAULT_PREFETCH_COUNT,
) -> list[tuple[DocumentSection, MusicPlan, MusicPlan | None]]:
    window = document.sections[start_index:start_index + count]
    previous_plan = None
    if start_index > 0:
        previous_plan = await analyze_section(
            document.sections[start_index - 1],
            api_key=api_key,
            reading_speed_wpm=reading_speed_wpm,
        )

    results: list[tuple[DocumentSection, MusicPlan, MusicPlan | None]] = []
    for section in window:
        plan = await analyze_section(
            section,
            api_key=api_key,
            reading_speed_wpm=reading_speed_wpm,
            previous_plan=previous_plan,
        )
        results.append((section, plan, previous_plan))
        previous_plan = plan

    return results


async def prefetch_section_audio(
    document: Document,
    *,
    api_key: str,
    reading_speed_wpm: int,
    start_index: int = 0,
    count: int = DEFAULT_PREFETCH_COUNT,
    planned_sections: list[tuple[DocumentSection, MusicPlan, MusicPlan | None]] | None = None,
) -> list[GeneratedClip]:
    if planned_sections is None:
        planned_sections = await build_section_plans(
            document,
            api_key=api_key,
            reading_speed_wpm=reading_speed_wpm,
            start_index=start_index,
            count=count,
        )
    clips: list[GeneratedClip] = []
    for section, plan, previous_plan in planned_sections:
        clip = await generate_section_audio(
            section,
            plan,
            api_key=api_key,
            reading_speed_wpm=reading_speed_wpm,
            previous_plan=previous_plan,
        )
        clips.append(clip)
    return clips
