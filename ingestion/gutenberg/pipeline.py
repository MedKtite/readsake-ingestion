"""Download and publish a cleaned Gutenberg reader copy."""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

import requests

from ..db import Db
from ..storage import StorageBackend
from ..documents.metadata import DocumentMetadata
from ..documents.reader_package import build_reader_package
from .cleaner import VERSION, clean_epub


USER_AGENT = "MarginaliaIngestion/1.0 (Gutenberg reader preparation)"


def epub_url(gutenberg_id: int) -> str:
    return f"https://www.gutenberg.org/cache/epub/{gutenberg_id}/pg{gutenberg_id}-images.epub"


def process_gutenberg_book(
    gutenberg_id: int, *, db: Db, storage: StorageBackend, work_dir: Path
) -> dict:
    source = work_dir / f"{gutenberg_id}.epub"
    cleaned = work_dir / f"{gutenberg_id}.clean.epub"
    work_dir.mkdir(parents=True, exist_ok=True)

    response = requests.get(
        epub_url(gutenberg_id),
        headers={"User-Agent": USER_AGENT},
        timeout=120,
    )
    response.raise_for_status()
    source.write_bytes(response.content)
    metadata = clean_epub(source, cleaned)

    package = work_dir / f"{gutenberg_id}.reader-v1.zip"
    build_reader_package(
        cleaned,
        "epub",
        DocumentMetadata(
            title=metadata.title,
            author=metadata.author,
            description=metadata.description,
            language=metadata.language,
            page_count=None,
            chapter_count=None,
            cover_path=None,
        ),
        package,
    )
    reader_key = f"gutenberg/{gutenberg_id}/reader-v5.zip"
    reader_url = storage.upload(str(package), reader_key, "application/zip")
    cover_url = None
    cover_key = None
    if metadata.cover_name:
        with zipfile.ZipFile(cleaned) as archive:
            cover_name = next((n for n in archive.namelist() if n.endswith(metadata.cover_name)), None)
            if cover_name:
                cover_path = work_dir / f"{gutenberg_id}.cover"
                cover_path.write_bytes(archive.read(cover_name))
                cover_key = f"gutenberg/{gutenberg_id}/{VERSION}.cover"
                cover_url = storage.upload(str(cover_path), cover_key, "image/jpeg")

    result = {
        "reader_key": reader_key,
        "reader_url": reader_url,
        "cover_url": cover_url,
        "cover_key": cover_key,
        "title": metadata.title,
        "author": metadata.author,
        "language": metadata.language,
        "description": metadata.description,
        "removed_boilerplate": metadata.removed_boilerplate,
        "processor_version": VERSION,
    }
    db.mark_gutenberg_processed(gutenberg_id, result)
    return result
