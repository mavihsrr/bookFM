from __future__ import annotations

import asyncio
import base64
import inspect
import logging
from array import array
from collections.abc import Mapping
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


def _obj_get(obj: object, key: str) -> object | None:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _apply_pcm_gain_s16le(data: bytes, gain: float) -> bytes:
    if gain >= 0.999:
        return data
    samples = array("h")
    samples.frombytes(data)
    for i in range(len(samples)):
        scaled = round(samples[i] * gain)
        if scaled > 32767:
            scaled = 32767
        elif scaled < -32768:
            scaled = -32768
        samples[i] = scaled
    return samples.tobytes()


def _decode_chunk_data(data: object) -> bytes | None:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        try:
            return base64.b64decode(data)
        except Exception:
            return None
    return None


def _extract_audio_payloads(message: object) -> list[bytes]:
    server_content = _obj_get(message, "server_content")
    
    # 1. Try audio_chunks first (standard for audio responses)
    chunks = _obj_get(server_content, "audio_chunks") if server_content else None
    if chunks:
        payloads = []
        for chunk in chunks:
            decoded = _decode_chunk_data(_obj_get(chunk, "data"))
            if decoded:
                payloads.append(decoded)
        if payloads:
            return payloads

    # 2. Try model_turn parts (standard for function calls or generic responses)
    model_turn = _obj_get(server_content, "model_turn") if server_content else None
    parts = _obj_get(model_turn, "parts") if model_turn else None
    if parts:
        payloads = []
        for part in parts:
            inline_data = _obj_get(part, "inline_data")
            decoded = _decode_chunk_data(_obj_get(inline_data, "data")) if inline_data else None
            if decoded:
                payloads.append(decoded)
        if payloads:
            return payloads

    # 3. Try direct message.data (fallback for certain SDK implementations)
    direct = _decode_chunk_data(_obj_get(message, "data"))
    if direct:
        return [direct]

    return []


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
    # Lock guidance to model default 4.0 and temperature to a flat 1.0 
    # to structurally prevent audio mode collapse and "cricket" artifacts.
    guidance = 4.0
    temperature = 1.0
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
    next_log_at = 256 * 1024
    no_payload_logs = 0
    try:
        async for message in session.receive():
            payloads = _extract_audio_payloads(message)
            if not payloads:
                if no_payload_logs < 3:
                    no_payload_logs += 1
                    log.info(
                        "[Lyria Stream] No audio payload in message #%d | type=%s | has_server_content=%s | has_data=%s",
                        no_payload_logs,
                        type(message).__name__,
                        _obj_get(message, "server_content") is not None,
                        _obj_get(message, "data") is not None,
                    )
                continue

            for data in payloads:
                if gain < 0.999:
                    data = _apply_pcm_gain_s16le(data, gain)
                if handle is not None:
                    handle.write(data)
                if on_chunk is not None:
                    result = on_chunk(data)
                    if inspect.isawaitable(result):
                        await result
                bytes_written += len(data)
                if bytes_written <= len(data):
                    log.info("[Lyria Stream] First audio bytes received: %d", len(data))
                if bytes_written >= next_log_at:
                    log.info("[Lyria Stream] Bytes streamed so far: %d", bytes_written)
                    next_log_at += 256 * 1024

            if bytes_written >= target_bytes:
                await session.stop()
                break
    finally:
        if handle is not None:
            handle.close()

    log.info("[Lyria Stream] Total bytes streamed: %d", bytes_written)

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
