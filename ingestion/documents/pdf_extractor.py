from __future__ import annotations

from pathlib import Path
from html import escape

import fitz

from .metadata import DocumentMetadata, clean_document_text, clean_text


def extract_pdf(source: Path, text_output: Path, cover_output: Path) -> DocumentMetadata:
    doc = fitz.open(source)
    metadata = doc.metadata or {}
    pages: list[str] = []
    for index, page in enumerate(doc):
        text = clean_document_text(page.get_text("text")).strip()
        if text:
            pages.append(f"<h2>Page {index + 1}</h2>\n<p>{escape(text)}</p>")

    text_output.parent.mkdir(parents=True, exist_ok=True)
    text_output.write_text("\n".join(pages), encoding="utf-8")

    cover_path = None
    if doc.page_count:
        images = doc[0].get_images(full=True)
        if images:
            image = doc.extract_image(images[0][0])
            cover_output.write_bytes(image["image"])
            cover_path = str(cover_output)
    doc.close()
    return DocumentMetadata(
        title=clean_text(metadata.get("title")),
        author=clean_text(metadata.get("author")),
        description=clean_text(metadata.get("subject")),
        language=clean_text(metadata.get("language")),
        page_count=len(pages),
        chapter_count=None,
        cover_path=cover_path,
    )
