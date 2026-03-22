from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, UploadFile

from .analysis import build_analysis_prompt
from .config import DEFAULT_CROSSFADE_SECONDS, OUTPUT_DIR
from .lyria_session import LyriaSessionManager
from .music import build_music_config, build_weighted_prompts
from .pipeline import build_section_plans, prepare_document


def clamp_index(index: int, total: int) -> int:
    if total <= 0:
        raise HTTPException(status_code=400, detail="No sections available.")
    return max(0, min(index, total - 1))


def validate_ext(name: str) -> Literal["txt", "epub"]:
    ext = Path(name).suffix.lower()
    if ext == ".txt":
        return "txt"
    if ext == ".epub":
        return "epub"
    raise HTTPException(status_code=400, detail="Only .txt and .epub uploads are supported.")


async def prepare_from_upload(
    upload: UploadFile,
    *,
    reading_speed_wpm: int,
    semantic: bool,
    embed_backend: str,
    embed_model: str | None,
):
    kind = validate_ext(upload.filename or "")
    suffix = ".txt" if kind == "txt" else ".epub"
    content = await upload.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)

    try:
        if kind == "txt":
            return await prepare_document(
                text_file=temp_path,
                reading_speed_wpm=reading_speed_wpm,
                semantic=semantic,
                embed_backend=embed_backend,
                embed_model=embed_model,
            )
        return await prepare_document(
            epub_file=temp_path,
            reading_speed_wpm=reading_speed_wpm,
            semantic=semantic,
            embed_backend=embed_backend,
            embed_model=embed_model,
        )
    finally:
        if temp_path.exists():
            temp_path.unlink()


def inspect_payload(document) -> dict:
    return {
        "title": document.title,
        "source_type": document.source_type,
        "sections": [
            {
                "index": section.index,
                "title": section.title,
                "word_count": section.word_count,
                "paragraph_count": section.paragraph_count,
                "estimated_seconds": section.estimated_seconds,
                "preview": section.text[:120],
                "text": section.text,
            }
            for section in document.sections
        ],
    }


async def generate_live_from_document(
    *,
    document,
    reading_speed_wpm: int,
    section_index: int,
    count: int,
    show_prompts: bool,
) -> dict:
    start = clamp_index(section_index, len(document.sections))
    planned_sections = await build_section_plans(
        document,
        reading_speed_wpm=reading_speed_wpm,
        start_index=start,
        count=count,
    )
    plans = [plan for _, plan, _ in planned_sections]
    durations = [max(12, sec.estimated_seconds) for sec, _, _ in planned_sections]
    min_duration = min(durations) if durations else 12
    crossfade_seconds = min(DEFAULT_CROSSFADE_SECONDS, max(2, int(min_duration * 0.25)))

    manager = LyriaSessionManager()
    live_clip = await manager.stream_sections(
        plans,
        reading_speed_wpm=reading_speed_wpm,
        durations=durations,
        output_pcm=OUTPUT_DIR / "live.pcm",
        crossfade_seconds=crossfade_seconds,
    )

    payload = {
        "mode": "live",
        "start_section_index": start,
        "section_count": len(planned_sections),
        "crossfade_seconds": crossfade_seconds,
        "pcm_path": str(live_clip.output_path),
        "wav_path": str(live_clip.output_path.with_suffix(".wav")),
        "bytes_written": live_clip.bytes_written,
        "duration_seconds": live_clip.duration_seconds,
        "sections": [
            {
                "section_index": section.index,
                "estimated_seconds": section.estimated_seconds,
                "stream_seconds": duration,
            }
            for (section, _, _), duration in zip(planned_sections, durations, strict=True)
        ],
    }
    if show_prompts:
        payload["model_inputs"] = [
            {
                "section_index": section.index,
                "analysis_prompt": build_analysis_prompt(section, reading_speed_wpm),
                "weighted_prompts": [
                    {"text": prompt.text, "weight": prompt.weight}
                    for prompt in build_weighted_prompts(plan, previous_plan=previous_plan)
                ],
                "music_config": build_music_config(plan, reading_speed_wpm).model_dump(exclude_none=True),
            }
            for section, plan, previous_plan in planned_sections
        ]
    return payload
