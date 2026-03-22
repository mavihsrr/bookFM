from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from google import genai
from google.genai import types

from .config import LYRIA_MODEL
from .models import GeneratedClip, MusicPlan
from .music import ChunkCallback, build_music_config, build_weighted_prompts, receive_audio_stream, render_wav_from_pcm

log = logging.getLogger(__name__)


def _blend_weights(prev: dict[str, float], nxt: dict[str, float], t: float) -> dict[str, float]:
    keys = set(prev) | set(nxt)
    blended: dict[str, float] = {}
    for key in keys:
        a = prev.get(key, 0.0)
        b = nxt.get(key, 0.0)
        w = (1.0 - t) * a + t * b
        if w != 0.0:
            blended[key] = w
    return blended


def _prompt_map(prompts) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in prompts:
        out[str(p.text)] = float(p.weight)
    return out


def _weighted_prompts_from_map(prompt_map: dict[str, float]) -> list[types.WeightedPrompt]:
    return [
        types.WeightedPrompt(text=text, weight=weight)
        for text, weight in prompt_map.items()
        if weight != 0.0
    ]


def _scale_prompt_map(prompt_map: dict[str, float], factor: float) -> dict[str, float]:
    return {text: weight * factor for text, weight in prompt_map.items()}


def _outro_prompt_map(plan: MusicPlan) -> dict[str, float]:
    prompt_map = _prompt_map(build_weighted_prompts(plan))
    prompt_map.update(
        {
            "clear musical ending over the next few seconds": 1.25,
            "finish the phrase and resolve softly": 1.15,
            "soft decrescendo": 1.05,
            "resolved cadence": 0.98,
            "gentle closing bars": 0.9,
            "sustained release": 0.82,
            "sparse final bars": 0.78,
            "let the music land gently": 0.72,
        }
    )
    return prompt_map


def _tail_prompt_map(plan: MusicPlan) -> dict[str, float]:
    prompt_map = _outro_prompt_map(plan)
    prompt_map.update(
        {
            "near-silence release": 0.5,
            "long soft tail": 0.46,
            "final resonance": 0.42,
            "minimal lingering texture": 0.34,
            "do not restart energy": 0.45,
            "do not introduce a new phrase": 0.52,
        }
    )
    return _scale_prompt_map(prompt_map, 0.62)


class LyriaSessionManager:
    def __init__(self) -> None:
        self._client = genai.Client(http_options={"api_version": "v1alpha"})

    async def stream_sections(
        self,
        plans: list[MusicPlan],
        *,
        reading_speed_wpm: int,
        durations: list[int],
        output_pcm: Path,
        crossfade_seconds: int,
        on_chunk: ChunkCallback | None = None,
        intro_seconds: int = 2,
        outro_seconds: int = 6,
        tail_seconds: int = 4,
    ) -> GeneratedClip:
        if not plans:
            raise ValueError("No plans to stream.")
        if len(plans) != len(durations):
            raise ValueError("plans and durations must have the same length.")

        output_pcm.parent.mkdir(parents=True, exist_ok=True)
        total_duration_seconds = sum(durations) + tail_seconds

        async with self._client.aio.live.music.connect(model=LYRIA_MODEL) as session:
            receiver_task = asyncio.create_task(
                receive_audio_stream(
                    session,
                    duration_seconds=total_duration_seconds,
                    output_file=output_pcm,
                    on_chunk=on_chunk,
                )
            )

            first_plan = plans[0]
            intro_seconds = max(1, min(intro_seconds, max(1, durations[0] // 4)))
            outro_seconds = max(3, min(outro_seconds, max(3, durations[-1] // 2)))
            tail_seconds = max(2, tail_seconds)
            intro_map = _scale_prompt_map(_prompt_map(build_weighted_prompts(first_plan)), 0.65)
            intro_map["soft opening"] = 0.45
            intro_map["gradual build"] = 0.4
            intro_map[f"shape the piece to resolve naturally in about {total_duration_seconds} seconds"] = 0.42

            log.info(
                "[LyriaSession] Starting stream | sections=%d total_duration=%ds crossfade=%ds",
                len(plans), total_duration_seconds, crossfade_seconds,
            )
            log.info(
                "[LyriaSession] Section 0 | composer_prompt=%r | mood=%s | genre=%s | instruments=%s | bpm=%s",
                first_plan.composer_prompt,
                ", ".join(first_plan.mood_tags),
                ", ".join(first_plan.genre_tags),
                ", ".join(first_plan.instruments),
                first_plan.bpm,
            )
            log.info(
                "[LyriaSession] Intro prompts:\n%s",
                "\n".join(f"  weight={w:.3f}  {t!r}" for t, w in intro_map.items()),
            )

            await session.set_weighted_prompts(prompts=_weighted_prompts_from_map(intro_map))
            await session.set_music_generation_config(config=build_music_config(first_plan, reading_speed_wpm))
            await session.play()
            await self._transition_prompt_maps(
                session,
                previous_map=intro_map,
                next_map=_prompt_map(build_weighted_prompts(first_plan)),
                seconds=intro_seconds,
            )

            prev_plan = first_plan
            for idx, (plan, seconds) in enumerate(zip(plans, durations, strict=True)):
                is_first = idx == 0
                is_last = idx == len(plans) - 1

                if plan is prev_plan and is_first:
                    sustain_seconds = max(0, seconds - intro_seconds)
                    if is_last:
                        sustain_seconds = max(0, sustain_seconds - outro_seconds)
                    await asyncio.sleep(sustain_seconds)
                    if is_last:
                        await self._transition_to_outro(
                            session,
                            plan=plan,
                            reading_speed_wpm=reading_speed_wpm,
                            seconds=min(outro_seconds, max(1, seconds - intro_seconds)),
                            tail_seconds=tail_seconds,
                        )
                    continue

                transition_seconds = min(crossfade_seconds, max(1, seconds // 3))
                log.info(
                    "[LyriaSession] Transitioning to section %d/%d | composer_prompt=%r | mood=%s | transition=%ds",
                    idx + 1, len(plans),
                    plan.composer_prompt,
                    ", ".join(plan.mood_tags),
                    transition_seconds,
                )
                await self._transition_prompts(
                    session,
                    previous_plan=prev_plan,
                    next_plan=plan,
                    seconds=transition_seconds,
                )
                sustain_seconds = max(0, seconds - transition_seconds)
                if is_last:
                    sustain_seconds = max(0, sustain_seconds - outro_seconds)
                await asyncio.sleep(sustain_seconds)
                if is_last:
                    await self._transition_to_outro(
                        session,
                        plan=plan,
                        reading_speed_wpm=reading_speed_wpm,
                        seconds=min(outro_seconds, max(1, seconds - transition_seconds)),
                        tail_seconds=tail_seconds,
                    )
                prev_plan = plan

            bytes_written = await receiver_task

        log.info(
            "[LyriaSession] Stream complete | bytes=%d | output=%s",
            bytes_written, output_pcm,
        )

        wav_path = output_pcm.with_suffix(".wav")
        await render_wav_from_pcm(output_pcm, wav_path)

        return GeneratedClip(
            section_index=0,
            output_path=output_pcm,
            bytes_written=bytes_written,
            duration_seconds=total_duration_seconds,
            crossfade_seconds=crossfade_seconds,
        )

    async def _transition_prompts(
        self,
        session,
        *,
        previous_plan: MusicPlan,
        next_plan: MusicPlan,
        seconds: int,
    ) -> None:
        prev_prompts = build_weighted_prompts(previous_plan)
        next_prompts = build_weighted_prompts(next_plan, previous_plan=previous_plan)

        await self._transition_prompt_maps(
            session,
            previous_map=_prompt_map(prev_prompts),
            next_map=_prompt_map(next_prompts),
            seconds=seconds,
        )

    async def _transition_prompt_maps(
        self,
        session,
        *,
        previous_map: dict[str, float],
        next_map: dict[str, float],
        seconds: int,
    ) -> None:
        if seconds <= 0:
            await session.set_weighted_prompts(prompts=_weighted_prompts_from_map(next_map))
            return

        steps = 6
        for i in range(1, steps + 1):
            t = i / steps
            blended = _blend_weights(previous_map, next_map, t)
            await session.set_weighted_prompts(prompts=_weighted_prompts_from_map(blended))
            await asyncio.sleep(max(0.01, seconds / steps))

    async def _transition_to_outro(
        self,
        session,
        *,
        plan: MusicPlan,
        reading_speed_wpm: int,
        seconds: int,
        tail_seconds: int,
    ) -> None:
        seconds = max(1, seconds)
        base_map = _prompt_map(build_weighted_prompts(plan))
        outro_map = _outro_prompt_map(plan)
        await session.set_music_generation_config(
            config=types.LiveMusicGenerationConfig(
                bpm=max(60, min(200, int(build_music_config(plan, reading_speed_wpm).bpm))),
                density=max(0.08, min(plan.density * 0.72, 0.75)),
                brightness=max(0.12, min(plan.brightness * 0.88, 0.85)),
                guidance=max(0.8, min(plan.guidance, 4.0)),
                temperature=min(plan.temperature, 1.1),
                music_generation_mode=types.MusicGenerationMode.QUALITY,
            )
        )
        await self._transition_prompt_maps(
            session,
            previous_map=base_map,
            next_map=outro_map,
            seconds=seconds,
        )
        await self._transition_prompt_maps(
            session,
            previous_map=outro_map,
            next_map=_tail_prompt_map(plan),
            seconds=tail_seconds,
        )
