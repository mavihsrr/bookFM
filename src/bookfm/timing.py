from __future__ import annotations

import math
from collections.abc import Sequence

from .config import (
    PER_SECTION_OVERHEAD_SECONDS,
    READING_PACE_BUFFER_RATIO,
    SECTION_MAX_STREAM_SECONDS,
    SECTION_MIN_STREAM_SECONDS,
)
from .models import DocumentSection


def estimate_stream_seconds(
    section: DocumentSection,
    *,
    reading_speed_wpm: int,
    pace_buffer_ratio: float = READING_PACE_BUFFER_RATIO,
    min_seconds: int = SECTION_MIN_STREAM_SECONDS,
    max_seconds: int = SECTION_MAX_STREAM_SECONDS,
    overhead_seconds: float = PER_SECTION_OVERHEAD_SECONDS,
) -> int:
    words_per_second = max(1.0, reading_speed_wpm / 60.0)
    base_seconds = section.word_count / words_per_second
    paragraph_pause = max(0, section.paragraph_count - 1) * 0.35
    raw = (base_seconds + paragraph_pause + overhead_seconds) * pace_buffer_ratio
    return max(min_seconds, min(max_seconds, int(math.ceil(raw))))


def build_stream_durations(
    sections: Sequence[DocumentSection],
    *,
    reading_speed_wpm: int,
) -> list[int]:
    return [
        estimate_stream_seconds(section, reading_speed_wpm=reading_speed_wpm)
        for section in sections
    ]
