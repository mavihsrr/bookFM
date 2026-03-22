from __future__ import annotations

import json
import logging
import re
from typing import Any
from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import DEFAULT_ANALYSIS_MODEL, MAX_ANALYSIS_CHARS, MAX_COMPOSER_PROMPT_CHARS
from .models import DocumentSection, MusicPlan

log = logging.getLogger(__name__)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class MusicPlanSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    composer_prompt: str = Field(min_length=4, max_length=MAX_COMPOSER_PROMPT_CHARS)
    mood_tags: list[str] = Field(min_length=1, max_length=5)
    genre_tags: list[str] = Field(min_length=0, max_length=3)
    instruments: list[str] = Field(min_length=0, max_length=4)
    bpm: float = Field(ge=60, le=200)
    density: float = Field(ge=0.0, le=1.0)
    brightness: float = Field(ge=0.0, le=1.0)
    guidance: float = Field(ge=0.0, le=6.0)
    temperature: float = Field(ge=0.0, le=3.0)


def response_schema_dict() -> dict:
    return {
        "type": "object",
        "properties": {
            "composer_prompt": {"type": "string", "maxLength": MAX_COMPOSER_PROMPT_CHARS},
            "mood_tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
            "genre_tags": {"type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 3},
            "instruments": {"type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 4},
            "bpm": {"type": "number", "minimum": 60, "maximum": 200},
            "density": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "brightness": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "guidance": {"type": "number", "minimum": 0.0, "maximum": 6.0},
            "temperature": {"type": "number", "minimum": 0.0, "maximum": 3.0},
        },
        "required": [
            "composer_prompt",
            "mood_tags",
            "genre_tags",
            "instruments",
            "bpm",
            "density",
            "brightness",
            "guidance",
            "temperature",
        ],
    }


def parse_json_object(text: str) -> dict:
    if not text:
        raise ValueError("Empty model output.")

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    direct = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if direct:
        return json.loads(direct.group(0))

    raise ValueError("No JSON object found in model output.")


def coerce_list(value: object, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return cleaned or fallback
    if isinstance(value, str):
        cleaned = [part.strip() for part in value.split(",") if part.strip()]
        return cleaned or fallback
    return fallback


def normalize_plan(payload: dict) -> MusicPlan:
    required = [
        "composer_prompt",
        "mood_tags",
        "genre_tags",
        "instruments",
        "bpm",
        "density",
        "brightness",
        "guidance",
        "temperature",
    ]
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"Missing required fields in model output: {missing}")

    sanitized = sanitize_payload(payload)
    validated = MusicPlanSchema.model_validate(sanitized).model_dump()
    return MusicPlan(
        composer_prompt=str(validated["composer_prompt"]).strip(),
        mood_tags=coerce_list(validated["mood_tags"], []),
        genre_tags=coerce_list(validated["genre_tags"], []),
        instruments=coerce_list(validated["instruments"], []),
        bpm=int(clamp(float(validated["bpm"]), 60, 200)),
        density=float(clamp(float(validated["density"]), 0.0, 1.0)),
        brightness=float(clamp(float(validated["brightness"]), 0.0, 1.0)),
        guidance=float(clamp(float(validated["guidance"]), 0.0, 6.0)),
        temperature=float(clamp(float(validated["temperature"]), 0.0, 3.0)),
    )


_NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+")


def _to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER_RE.search(value)
        if match:
            return float(match.group(0))
    raise ValueError(f"Expected number, got {value!r}")


def _trim_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    sliced = text[:limit].rstrip()
    cut = max(sliced.rfind("."), sliced.rfind("!"), sliced.rfind("?"))
    if cut >= int(limit * 0.6):
        return sliced[: cut + 1].strip()
    cut_space = sliced.rfind(" ")
    if cut_space > 0:
        return sliced[:cut_space].strip()
    return sliced


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    # Deterministic sanitization: truncate lists and clamp numeric ranges.
    cleaned: dict[str, Any] = dict(payload)

    cleaned["composer_prompt"] = _trim_sentence(
        str(cleaned.get("composer_prompt", "")).strip(),
        MAX_COMPOSER_PROMPT_CHARS,
    )

    def trunc_list(key: str, max_items: int) -> None:
        value = cleaned.get(key, [])
        if isinstance(value, list):
            cleaned[key] = [str(x).strip() for x in value if str(x).strip()][:max_items]
        else:
            cleaned[key] = [str(value).strip()] if str(value).strip() else []

    trunc_list("mood_tags", 5)
    trunc_list("genre_tags", 3)
    trunc_list("instruments", 4)

    cleaned["bpm"] = clamp(_to_float(cleaned.get("bpm")), 60, 200)
    cleaned["density"] = clamp(_to_float(cleaned.get("density")), 0.0, 1.0)
    cleaned["brightness"] = clamp(_to_float(cleaned.get("brightness")), 0.0, 1.0)
    cleaned["guidance"] = clamp(_to_float(cleaned.get("guidance")), 0.0, 6.0)
    cleaned["temperature"] = clamp(_to_float(cleaned.get("temperature")), 0.0, 3.0)

    return cleaned


def build_analysis_prompt(section: DocumentSection, reading_speed_wpm: int) -> str:
    current_text = section.text[:MAX_ANALYSIS_CHARS]
    return (
        "You are an expert reading-music composer. "
        "Create an instrumental soundtrack plan that matches this section's narrative tone and pacing. "
        "Do not lock to any fixed style, genre, or instrument family unless the text suggests it. "
        "Do not default to piano, ambient pads, cinematic strings, or soft orchestral music unless the writing itself points there. "
        "When the setting, motion, emotional temperature, or implied era changes, let the palette change too. "
        "Take concrete cues from the text: environment, tension, stillness, movement, period feel, and scale. "
        "Primary objective: this must accompany reading comfortably. "
        "Keep it suitable for reading: coherent, non-chaotic, emotionally aligned, and non-intrusive over long listening. "
        "Avoid abrupt transitions, distracting motifs, and sudden loud/intense shifts that break reading focus. "
        "Avoid reusing the same default answer shape across unrelated passages. "
        "Return a JSON object that matches the provided response schema. "
        f"Reader speed is {reading_speed_wpm} words per minute. "
        "Use nearby context only to improve transitions, not to dominate the mood.\n\n"
        f"SECTION_TITLE:\n{section.title}\n\n"
        f"PREVIOUS_CONTEXT:\n{section.context_before}\n\n"
        f"CURRENT_SECTION:\n{current_text}\n\n"
        f"NEXT_CONTEXT:\n{section.context_after}"
    )


async def analyze_section(
    section: DocumentSection,
    *,
    reading_speed_wpm: int,
    previous_plan: MusicPlan | None = None,
    analysis_model: str = DEFAULT_ANALYSIS_MODEL,
) -> MusicPlan:
    client = genai.Client()
    prompt = build_analysis_prompt(section, reading_speed_wpm)
    if previous_plan is not None:
        prompt += (
            "\n\nPREVIOUS_MUSIC_PLAN:\n"
            f"prompt={previous_plan.composer_prompt}; "
            f"mood={', '.join(previous_plan.mood_tags)}; "
            f"genre={', '.join(previous_plan.genre_tags)}; "
            f"instruments={', '.join(previous_plan.instruments)}; "
            f"bpm={previous_plan.bpm}. "
            "Transition gently from this palette instead of sharply changing tone, "
            "but do not stay trapped in the same palette if the new section clearly wants something different."
        )
    response = await client.aio.models.generate_content(
        model=analysis_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=response_schema_dict(),
        ),
    )
    try:
        payload = parse_json_object(response.text or "")
        plan = normalize_plan(payload)
        log.info(
            "[Analysis] Section %d | title=%r | composer_prompt=%r",
            section.index, section.title, plan.composer_prompt,
        )
        log.info(
            "[Analysis] Section %d | mood=%s | genre=%s | instruments=%s",
            section.index,
            ", ".join(plan.mood_tags),
            ", ".join(plan.genre_tags),
            ", ".join(plan.instruments),
        )
        log.info(
            "[Analysis] Section %d | bpm=%s density=%.3f brightness=%.3f guidance=%.3f temperature=%.3f",
            section.index, plan.bpm, plan.density, plan.brightness, plan.guidance, plan.temperature,
        )
        return plan
    except (ValueError, ValidationError) as exc:
        raise ValueError(f"Model output did not match the music plan schema: {response.text}") from exc
