from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def clean_document_text(value: object) -> str:
    """Remove characters XML/XHTML cannot represent safely."""
    text = str(value or "")
    return "".join(
        char for char in text
        if char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD
        or 0x10000 <= ord(char) <= 0x10FFFF
    )


@dataclass(frozen=True)
class DocumentMetadata:
    title: Optional[str]
    author: Optional[str]
    description: Optional[str]
    language: Optional[str]
    page_count: Optional[int]
    chapter_count: Optional[int]
    cover_path: Optional[str]


def clean_text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(clean_document_text(value).split())
    return text or None
