from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from dataclasses import asdict

from .analysis import analyze_section, build_analysis_prompt
from .config import (
    DEFAULT_CROSSFADE_SECONDS,
    DEFAULT_OPENAI_EMBED_MODEL,
    DEFAULT_PREFETCH_COUNT,
    DEFAULT_READING_SPEED_WPM,
)
from .config import DEFAULT_EMBED_MODEL
from .lyria_session import LyriaSessionManager
from .music import build_music_config, build_weighted_prompts
from .pipeline import build_section_plans, prepare_document, prefetch_section_audio


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="bookfm adaptive reading music prototype")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_source_args(target: argparse.ArgumentParser) -> None:
        source_group = target.add_mutually_exclusive_group(required=True)
        source_group.add_argument("--text")
        source_group.add_argument("--text-file", type=Path)
        source_group.add_argument("--epub-file", type=Path)
        target.add_argument("--reading-speed-wpm", type=int, default=DEFAULT_READING_SPEED_WPM)
        target.add_argument("--semantic", action="store_true", help="Use embedding-based semantic breaks (extra cost).")
        target.add_argument(
            "--embed-backend",
            choices=["openai", "google"],
            default="openai",
            help="Embedding provider for --semantic mode.",
        )
        target.add_argument(
            "--embed-model",
            default=None,
            help=f"Embedding model name (default: {DEFAULT_OPENAI_EMBED_MODEL} for openai, {DEFAULT_EMBED_MODEL} for google).",
        )

    inspect_parser = subparsers.add_parser("inspect", help="Load and chunk a document")
    add_source_args(inspect_parser)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze one section into a music plan")
    add_source_args(analyze_parser)
    analyze_parser.add_argument("--section-index", type=int, default=0)
    analyze_parser.add_argument("--show-prompts", action="store_true")

    generate_parser = subparsers.add_parser("generate", help="Generate audio for one section")
    add_source_args(generate_parser)
    generate_parser.add_argument("--section-index", type=int, default=0)
    generate_parser.add_argument("--count", type=int, default=DEFAULT_PREFETCH_COUNT)
    generate_parser.add_argument("--show-prompts", action="store_true")

    prefetch_parser = subparsers.add_parser("prefetch", help="Generate audio for upcoming sections")
    add_source_args(prefetch_parser)
    prefetch_parser.add_argument("--start-index", type=int, default=0)
    prefetch_parser.add_argument("--count", type=int, default=DEFAULT_PREFETCH_COUNT)

    return parser.parse_args()


def _source_kwargs(args: argparse.Namespace) -> dict:
    return {
        "text": getattr(args, "text", None),
        "text_file": getattr(args, "text_file", None),
        "epub_file": getattr(args, "epub_file", None),
    }


async def _run_async(args: argparse.Namespace) -> None:
    document = await prepare_document(
        **_source_kwargs(args),
        reading_speed_wpm=args.reading_speed_wpm,
        semantic=getattr(args, "semantic", False),
        embed_backend=getattr(args, "embed_backend", "openai"),
        embed_model=getattr(args, "embed_model", None),
    )

    if not document.sections:
        raise RuntimeError("No readable sections were created from the input.")

    if args.command == "inspect":
        payload = {
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
                }
                for section in document.sections
            ],
        }
        print(json.dumps(payload, indent=2))
        return

    section = document.sections[getattr(args, "section_index", 0)] if args.command in {"analyze", "generate"} else None

    if args.command == "analyze":
        plan = await analyze_section(section, reading_speed_wpm=args.reading_speed_wpm)
        print(json.dumps(asdict(plan), indent=2))
        if args.show_prompts:
            print("\nAnalysis prompt:\n")
            print(build_analysis_prompt(section, args.reading_speed_wpm))
        return

    if args.command == "generate":
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError("Missing GEMINI_API_KEY in environment or .env file.")
        planned_sections = await build_section_plans(
            document,
            reading_speed_wpm=args.reading_speed_wpm,
            start_index=section.index,
            count=args.count,
        )
        plans = [plan for _, plan, _ in planned_sections]
        durations = [max(12, sec.estimated_seconds) for sec, _, _ in planned_sections]
        min_duration = min(durations) if durations else 12
        crossfade_seconds = min(DEFAULT_CROSSFADE_SECONDS, max(2, int(min_duration * 0.25)))

        manager = LyriaSessionManager()
        live_clip = await manager.stream_sections(
            plans,
            reading_speed_wpm=args.reading_speed_wpm,
            durations=durations,
            output_pcm=Path(".bookfm_output/live.pcm"),
            crossfade_seconds=crossfade_seconds,
        )
        print(
            json.dumps(
                {
                    "mode": "live",
                    "start_section_index": section.index,
                    "section_count": args.count,
                    "crossfade_seconds": crossfade_seconds,
                    "pcm_path": str(live_clip.output_path),
                    "wav_path": str(live_clip.output_path.with_suffix(".wav")),
                    "bytes_written": live_clip.bytes_written,
                    "duration_seconds": live_clip.duration_seconds,
                    "sections": [
                        {
                            "section_index": planned_section.index,
                            "estimated_seconds": planned_section.estimated_seconds,
                            "stream_seconds": duration,
                        }
                        for (planned_section, _, _), duration in zip(planned_sections, durations, strict=True)
                    ],
                },
                indent=2,
            )
        )
        if args.show_prompts:
            debug_payload = []
            for planned_section, plan, previous_plan in planned_sections:
                debug_payload.append(
                    {
                        "section_index": planned_section.index,
                        "analysis_prompt": build_analysis_prompt(planned_section, args.reading_speed_wpm),
                        "weighted_prompts": [
                            {"text": prompt.text, "weight": prompt.weight}
                            for prompt in build_weighted_prompts(plan, previous_plan=previous_plan)
                        ],
                        "music_config": build_music_config(plan, args.reading_speed_wpm).model_dump(exclude_none=True),
                    }
                )
            print("\nModel inputs:\n")
            print(json.dumps(debug_payload, indent=2))
        print(f"\nPlay WAV: ffplay -nodisp -autoexit {live_clip.output_path.with_suffix('.wav')}")
        print(f"Play PCM: ffplay -f s16le -ar 48000 -ac 2 -nodisp -autoexit {live_clip.output_path}")
        return

    if args.command == "prefetch":
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError("Missing GEMINI_API_KEY in environment or .env file.")
        clips = await prefetch_section_audio(
            document,
            reading_speed_wpm=args.reading_speed_wpm,
            start_index=args.start_index,
            count=args.count,
        )
        print(json.dumps([
            {
                "section_index": clip.section_index,
                "output_path": str(clip.output_path),
                "bytes_written": clip.bytes_written,
            }
            for clip in clips
        ], indent=2))


def run() -> None:
    asyncio.run(_run_async(parse_args()))


if __name__ == "__main__":
    run()
