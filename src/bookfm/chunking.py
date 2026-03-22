from __future__ import annotations

import re

from .config import (
    DEFAULT_READING_SPEED_WPM,
    DEFAULT_SECTION_MAX_CHARS,
    DEFAULT_SECTION_MAX_SECONDS,
    DEFAULT_SECTION_MIN_SECONDS,
    DEFAULT_SECTION_TARGET_SECONDS,
)
from .models import Document, DocumentSection


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]


def _sentences(text: str) -> list[str]:
    # Also split when punctuation is immediately followed by uppercase text (e.g. "foo.Bar").
    parts = re.split(r"(?<=[.!?])(?:\s+|(?=[A-Z\"']))", text.strip())
    return [part.strip() for part in parts if part.strip()]


def _blocks(text: str, max_chars: int) -> list[str]:
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return _sentences(text)
    if len(paragraphs) == 1:
        single = paragraphs[0]
        sentences = _sentences(single)
        if len(sentences) > 1:
            paragraphs = sentences

    blocks: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            blocks.append(paragraph)
            continue

        sentences = _sentences(paragraph)
        if not sentences:
            blocks.append(paragraph)
            continue

        current_parts: list[str] = []
        for sentence in sentences:
            projected = " ".join([*current_parts, sentence]).strip()
            if current_parts and len(projected) > max_chars:
                blocks.append(" ".join(current_parts).strip())
                current_parts = [sentence]
                continue
            current_parts.append(sentence)

        if current_parts:
            blocks.append(" ".join(current_parts).strip())

    return blocks


def split_blocks(text: str, *, max_chars: int) -> list[str]:
    return _blocks(text, max_chars=max_chars)


def _expand_oversized_block(
    block: str,
    *,
    max_words: int,
    max_seconds: int,
    max_chars: int,
    reading_speed_wpm: int,
) -> list[str]:
    words = len(block.split())
    seconds = _estimate_seconds(words, reading_speed_wpm)
    if words <= max_words and seconds <= max_seconds and len(block) <= max_chars:
        return [block]

    sentences = _sentences(block)
    if len(sentences) <= 1:
        return [block]

    parts: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        projected = " ".join([*current, sentence]).strip()
        projected_words = len(projected.split())
        projected_seconds = _estimate_seconds(projected_words, reading_speed_wpm)
        if current and (
            projected_words > max_words
            or projected_seconds > max_seconds
            or len(projected) > max_chars
        ):
            parts.append(" ".join(current).strip())
            current = [sentence]
            continue
        current.append(sentence)

    if current:
        parts.append(" ".join(current).strip())

    return parts or [block]


def _estimate_seconds(word_count: int, reading_speed_wpm: int) -> int:
    words_per_second = max(reading_speed_wpm, 1) / 60.0
    return max(6, round(word_count / words_per_second))


def _target_words(reading_speed_wpm: int, seconds: int) -> int:
    return max(40, round((reading_speed_wpm / 60.0) * seconds))


def chunk_document(
    document: Document,
    *,
    reading_speed_wpm: int = DEFAULT_READING_SPEED_WPM,
    target_seconds: int = DEFAULT_SECTION_TARGET_SECONDS,
    min_seconds: int = DEFAULT_SECTION_MIN_SECONDS,
    max_seconds: int = DEFAULT_SECTION_MAX_SECONDS,
    max_chars: int = DEFAULT_SECTION_MAX_CHARS,
) -> Document:
    blocks = _blocks(document.full_text, max_chars=max_chars)
    if not blocks:
        document.sections = []
        return document

    sections: list[DocumentSection] = []
    current_title = "Section 1"
    current_parts: list[str] = []
    target_words = _target_words(reading_speed_wpm, target_seconds)
    min_words = _target_words(reading_speed_wpm, min_seconds)
    max_words = _target_words(reading_speed_wpm, max_seconds)

    def flush() -> None:
        if not current_parts:
            return
        text = "\n\n".join(current_parts).strip()
        word_count = len(text.split())
        sections.append(
            DocumentSection(
                index=len(sections),
                title=current_title,
                text=text,
                word_count=word_count,
                estimated_seconds=_estimate_seconds(word_count, reading_speed_wpm),
                paragraph_count=len(current_parts),
            )
        )

    expanded_blocks: list[str] = []
    for block in blocks:
        expanded_blocks.extend(
            _expand_oversized_block(
                block,
                max_words=max_words,
                max_seconds=max_seconds,
                max_chars=max_chars,
                reading_speed_wpm=reading_speed_wpm,
            )
        )

    for block in expanded_blocks:
        separator = "\n\n" if current_parts else ""
        projected = f"{separator.join(current_parts)}{separator}{block}".strip()
        projected_words = len(projected.split())
        projected_seconds = _estimate_seconds(projected_words, reading_speed_wpm)
        if current_parts and (
            projected_words > max_words
            or projected_seconds > max_seconds
            or len(projected) > max_chars
        ):
            flush()
            current_parts = [block]
            current_title = f"Section {len(sections) + 1}"
            continue

        current_parts.append(block)

    flush()

    # If the last section is too small, merge it backward for smoother flow.
    if len(sections) >= 2:
        last = sections[-1]
        if last.word_count < min_words:
            prev = sections[-2]
            merged_text = f"{prev.text}\n\n{last.text}".strip()
            merged_words = len(merged_text.split())
            merged_section = DocumentSection(
                index=prev.index,
                title=prev.title,
                text=merged_text,
                word_count=merged_words,
                estimated_seconds=_estimate_seconds(merged_words, reading_speed_wpm),
                paragraph_count=prev.paragraph_count + last.paragraph_count,
            )
            sections = sections[:-2] + [merged_section]

    for index, section in enumerate(sections):
        before = sections[index - 1].text[:200] if index > 0 else ""
        after = sections[index + 1].text[:200] if index + 1 < len(sections) else ""
        section.context_before = before
        section.context_after = after
        if not section.title:
            section.title = f"Section {index + 1}"

    document.sections = sections
    return document
