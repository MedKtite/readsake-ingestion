from __future__ import annotations

import json
from pathlib import Path

from ..db import Db
from ..storage import StorageBackend
from .reader_package import build_reader_package
from .epub_extractor import extract_epub
from .metadata import DocumentMetadata
from .pdf_extractor import extract_pdf


VERSION = "reader-package-v1"


def process_uploaded_document(book_id: str, source_key: str, fmt: str, *, db: Db, storage: StorageBackend, work_dir: Path) -> dict:
    source = work_dir / f"original.{fmt}"
    extracted = work_dir / "extracted.html"
    cover = work_dir / "cover.jpg"
    reader = work_dir / "reader-v1.zip"
    work_dir.mkdir(parents=True, exist_ok=True)
    storage.download(source_key, str(source))

    if fmt.lower() == "pdf":
        metadata = extract_pdf(source, extracted, cover)
    elif fmt.lower() == "epub":
        metadata = extract_epub(source, extracted, cover)
    else:
        raise ValueError(f"Unsupported document format: {fmt}")

    build_reader_package(source, fmt, metadata, reader)
    prefix = f"books/{book_id}/{VERSION}"
    reader_key = f"{prefix}/reader-v1.zip"
    reader_url = storage.upload(str(reader), reader_key, "application/zip")
    cover_key = None
    cover_url = None
    if metadata.cover_path:
        cover_key = f"{prefix}/cover.jpg"
        storage.upload(metadata.cover_path, cover_key, "image/jpeg")
        # The bucket is private: the backend streams the cover from cover_key.
        cover_url = f"/covers/books/{book_id}"

    result = {
        "reader_key": reader_key,
        "reader_url": reader_url,
        "cover_key": cover_key,
        "cover_url": cover_url,
        "title": metadata.title,
        "author": metadata.author,
        "description": metadata.description,
        "language": metadata.language,
        "page_count": metadata.page_count,
        "chapter_count": metadata.chapter_count,
        "processor_version": VERSION,
    }
    db.mark_uploaded_processed(book_id, result)
    return result
