from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .config import DEFAULT_PREFETCH_COUNT, DEFAULT_READING_SPEED_WPM


class BaseTextRequest(BaseModel):
    text: str = Field(min_length=1)
    reading_speed_wpm: int = DEFAULT_READING_SPEED_WPM
    semantic: bool = False
    embed_backend: Literal["openai", "google"] = "openai"
    embed_model: str | None = None


class GenerateTextRequest(BaseTextRequest):
    section_index: int = 0
    count: int = DEFAULT_PREFETCH_COUNT
    show_prompts: bool = False
