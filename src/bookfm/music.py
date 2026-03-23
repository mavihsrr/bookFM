from __future__ import annotations

import asyncio
import audioop
import inspect
import logging
from pathlib import Path
from typing import Awaitable, Callable

from google import genai
from google.genai import types

from .config import (
    BYTES_PER_SAMPLE,
    CHANNELS,
    DEFAULT_CROSSFADE_SECONDS,
    DEFAULT_STREAM_GAIN,
    LYRIA_MODEL,
    OUTPUT_DIR,
    SAMPLE_RATE,
)
from .models import DocumentSection, GeneratedClip, MusicPlan

log = logging.getLogger(__name__)


ChunkCallback = Callable[[bytes], Awaitable[None] | None]


def blend_bpm(story_bpm: int, reading_speed_wpm: int) -> int:
    speed_mapped = max(60, min(180, int(70 + (reading_speed_wpm - 120) * 0.35)))
    # Keep section-derived tempo as the primary source; reading speed is a light nudge only.
    return max(60, min(200, int(round((story_bpm * 0.9) + (speed_mapped * 0.1)))))


def build_weighted_prompts(plan: MusicPlan, previous_plan: MusicPlan | None = None) -> list[types.WeightedPrompt]:
    prompts = [types.WeightedPrompt(text=plan.composer_prompt, weight=2.25)]
    prompts.append(
        types.WeightedPrompt(
            text="background score for reading: supportive, subtle, and context-led",
            weight=0.28,
        )
    )
    prompts.append(
        types.WeightedPrompt(
            text="instrumental only, avoid literal scene sound effects",
            weight=0.2,
        )
    )
    prompts.append(
        types.WeightedPrompt(
            text="clean mix, smooth texture, avoid crackle or harsh distortion artifacts",
            weight=0.24,
        )
    )
    prompts.extend(types.WeightedPrompt(text=tag, weight=1.15) for tag in plan.mood_tags[:4])
    prompts.extend(types.WeightedPrompt(text=tag, weight=0.78) for tag in plan.genre_tags[:2])
    prompts.extend(types.WeightedPrompt(text=tag, weight=0.68) for tag in plan.instruments[:3])
    prompts.append(
        types.WeightedPrompt(
            text="preserve the emotional tone of the text and avoid abrupt jumps",
            weight=0.22,
        )
    )
    if previous_plan is not None:
        overlap = len(set(plan.mood_tags) & set(previous_plan.mood_tags))
        carry_weight = 0.08 if overlap > 0 else 0.04
        prompts.extend(types.WeightedPrompt(text=tag, weight=carry_weight) for tag in previous_plan.mood_tags[:2])
        prompts.extend(types.WeightedPrompt(text=tag, weight=carry_weight * 0.7) for tag in previous_plan.genre_tags[:1])
    return prompts


def build_music_config(plan: MusicPlan, reading_speed_wpm: int) -> types.LiveMusicGenerationConfig:
    bpm = max(60, min(200, blend_bpm(plan.bpm, reading_speed_wpm)))
    density = min(plan.density, 0.58)
    brightness = min(plan.brightness, 0.52)
    guidance = max(1.0, min(plan.guidance, 3.6))
    temperature = min(plan.temperature, 1.35)
    log.info(
        "[Lyria Config] bpm=%s density=%.3f brightness=%.3f guidance=%.3f temperature=%.3f",
        bpm, density, brightness, guidance, temperature,
    )
    try:
        return types.LiveMusicGenerationConfig(
            bpm=bpm,
            density=density,
            brightness=brightness,
            guidance=guidance,
            temperature=temperature,
            music_generation_mode=types.MusicGenerationMode.QUALITY,
        )
    except TypeError:
        return types.LiveMusicGenerationConfig(
            bpm=bpm,
            temperature=temperature,
        )


async def receive_audio_stream(
    session,
    *,
    duration_seconds: int,
    output_file: Path | None = None,
    on_chunk: ChunkCallback | None = None,
) -> int:
    bytes_per_second = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
    target_bytes = bytes_per_second * duration_seconds
    bytes_written = 0

    handle = output_file.open("wb") if output_file is not None else None
    gain = float(DEFAULT_STREAM_GAIN)
    try:
        async for message in session.receive():
            server_content = getattr(message, "server_content", None)
            chunks = getattr(server_content, "audio_chunks", None) if server_content else None
            if not chunks:
                continue

            for chunk in chunks:
                data = getattr(chunk, "data", None)
                if not data:
                    continue
                if gain < 0.999:
                    data = audioop.mul(data, BYTES_PER_SAMPLE, gain)
                if handle is not None:
                    handle.write(data)
                if on_chunk is not None:
                    result = on_chunk(data)
                    if inspect.isawaitable(result):
                        await result
                bytes_written += len(data)

            if bytes_written >= target_bytes:
                await session.stop()
                break
    finally:
        if handle is not None:
            handle.close()

    return bytes_written


async def receive_audio_to_file(session, output_file: Path, duration_seconds: int) -> int:
    return await receive_audio_stream(
        session,
        duration_seconds=duration_seconds,
        output_file=output_file,
    )


async def render_wav_from_pcm(pcm_path: Path, wav_path: Path) -> None:
    # Convert raw PCM to WAV and apply gentle mastering so it's actually listenable.
    # Filters: remove sub-rumble, tame harsh highs, normalize loudness, and prevent clipping.
    filt = "highpass=f=35,lowpass=f=12000,loudnorm=I=-16:LRA=8:TP=-1.5,alimiter=limit=0.9"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-f",
        "s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-i",
        str(pcm_path),
        "-af",
        filt,
        str(wav_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError("ffmpeg failed to render WAV from PCM")


async def generate_section_audio(
    section: DocumentSection,
    plan: MusicPlan,
    *,
    api_key: str,
    reading_speed_wpm: int,
    duration_seconds: int | None = None,
    previous_plan: MusicPlan | None = None,
) -> GeneratedClip:
    clip_seconds = duration_seconds or max(18, min(section.estimated_seconds, 42))
    output_path = OUTPUT_DIR / f"section_{section.index}.pcm"

    client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})
    prompts = build_weighted_prompts(plan, previous_plan=previous_plan)
    config = build_music_config(plan, reading_speed_wpm)

    log.info(
        "[Lyria] Generating section %d | duration=%ds | composer_prompt=%r",
        section.index, clip_seconds, plan.composer_prompt,
    )
    log.info(
        "[Lyria] Weighted prompts:\n%s",
        "\n".join(f"  weight={p.weight:.3f}  {p.text!r}" for p in prompts),
    )

    async with client.aio.live.music.connect(model=LYRIA_MODEL) as session:
        receiver_task = asyncio.create_task(
            receive_audio_stream(
                session,
                duration_seconds=clip_seconds,
                output_file=output_path,
            )
        )
        await session.set_weighted_prompts(prompts=prompts)
        await session.set_music_generation_config(config=config)
        await session.play()
        bytes_written = await receiver_task

    log.info(
        "[Lyria] Section %d done | bytes=%d | duration=%.1fs",
        section.index, bytes_written, bytes_written / (SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE),
    )

    # Always render a WAV alongside PCM so you can play it normally.
    wav_path = output_path.with_suffix(".wav")
    await render_wav_from_pcm(output_path, wav_path)

    return GeneratedClip(
        section_index=section.index,
        output_path=output_path,
        bytes_written=bytes_written,
        duration_seconds=clip_seconds,
        crossfade_seconds=min(DEFAULT_CROSSFADE_SECONDS, max(3, clip_seconds // 5)),
    )
