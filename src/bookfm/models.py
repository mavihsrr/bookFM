from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class DocumentSection:
    index: int
    title: str
    text: str
    word_count: int
    estimated_seconds: int
    paragraph_count: int = 0
    context_before: str = ""
    context_after: str = ""


@dataclass(slots=True)
class Document:
    source_type: str
    source_name: str
    title: str
    full_text: str
    sections: list[DocumentSection] = field(default_factory=list)


@dataclass(slots=True)
class MusicPlan:
    composer_prompt: str
    mood_tags: list[str]
    genre_tags: list[str]
    instruments: list[str]
    bpm: int
    density: float
    brightness: float
    guidance: float
    temperature: float


@dataclass(slots=True)
class GeneratedClip:
    section_index: int
    output_path: Path
    bytes_written: int
    duration_seconds: int
    crossfade_seconds: int = 4
