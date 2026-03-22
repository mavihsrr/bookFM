from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from pathlib import Path
from zipfile import ZipFile

from .models import Document


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return " ".join(self.parts)


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _read_text_file(path: Path) -> str:
    return _normalize_text(path.read_text(encoding="utf-8"))


def _read_epub_file(path: Path) -> str:
    sections: list[str] = []
    with ZipFile(path) as archive:
        html_names = [
            name for name in archive.namelist()
            if name.endswith((".xhtml", ".html", ".htm")) and not name.startswith("META-INF/")
        ]
        for name in sorted(html_names):
            raw = archive.read(name).decode("utf-8", errors="ignore")
            parser = _HTMLTextExtractor()
            parser.feed(raw)
            text = parser.text()
            if text:
                sections.append(text)
    return _normalize_text("\n\n".join(sections))


async def load_document(
    *,
    text: str | None = None,
    text_file: Path | None = None,
    epub_file: Path | None = None,
) -> Document:
    provided = [value is not None for value in (text, text_file, epub_file)]
    if sum(provided) != 1:
        raise ValueError("Provide exactly one source: text, text_file, or epub_file.")

    if text is not None:
        normalized = _normalize_text(text)
        if not normalized:
            raise ValueError("Input text is empty.")
        return Document(
            source_type="text",
            source_name="inline",
            title="Pasted text",
            full_text=normalized,
        )

    if text_file is not None:
        normalized = await asyncio.to_thread(_read_text_file, text_file)
        if not normalized:
            raise ValueError("Text file is empty.")
        return Document(
            source_type="txt",
            source_name=text_file.name,
            title=text_file.stem,
            full_text=normalized,
        )

    normalized = await asyncio.to_thread(_read_epub_file, epub_file)
    if not normalized:
        raise ValueError("EPUB file appears to be empty.")
    return Document(
        source_type="epub",
        source_name=epub_file.name,
        title=epub_file.stem,
        full_text=normalized,
    )
