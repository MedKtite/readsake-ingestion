"""Download and publish a cleaned Gutenberg reader copy."""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import requests

from ..db import Db
from ..storage import StorageBackend
from ..documents.metadata import DocumentMetadata
from ..documents.reader_package import build_reader_package
from .cleaner import VERSION, clean_epub


USER_AGENT = "ReadsakeIngestion/1.0 (Gutenberg reader preparation)"
# PGLAF's mirror serves the same cache paths and stays up when www.gutenberg.org
# returns 503s; Gutenberg also asks automated clients to prefer mirrors.
GUTENBERG_HOSTS = [
    host.strip().rstrip("/")
    for host in os.getenv("GUTENBERG_HOSTS", "https://gutenberg.pglaf.org,https://www.gutenberg.org").split(",")
    if host.strip()
]


def epub_url(gutenberg_id: int, host: str = GUTENBERG_HOSTS[0]) -> str:
    return f"{host}/cache/epub/{gutenberg_id}/pg{gutenberg_id}-images.epub"


def _download_epub(gutenberg_id: int, destination: Path) -> None:
    """Fetch the EPUB from the first host that returns a valid file."""
    errors = []
    for host in GUTENBERG_HOSTS:
        try:
            response = requests.get(epub_url(gutenberg_id, host),
                                    headers={"User-Agent": USER_AGENT}, timeout=(10, 90))
            response.raise_for_status()
        except requests.RequestException as exc:
            errors.append(f"{host}: {exc}")
            continue
        destination.write_bytes(response.content)
        if zipfile.is_zipfile(destination):
            return
        errors.append(f"{host}: invalid EPUB")
    raise RuntimeError("SOURCE_UNAVAILABLE: Gutenberg could not supply the book. Retry later. "
                       + "; ".join(errors))


def process_gutenberg_book(
    gutenberg_id: int, *, db: Db, storage: StorageBackend, work_dir: Path
) -> dict:
    source = work_dir / f"{gutenberg_id}.epub"
    cleaned = work_dir / f"{gutenberg_id}.clean.epub"
    work_dir.mkdir(parents=True, exist_ok=True)

    source_key = f"gutenberg/{gutenberg_id}/source.epub"
    if storage.exists(source_key):
        storage.download(source_key, str(source))
    else:
        _download_epub(gutenberg_id, source)
        storage.upload(str(source), source_key, "application/epub+zip")
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
    digest = hashlib.sha256(package.read_bytes()).hexdigest()[:16]
    reader_key = f"gutenberg/{gutenberg_id}/reader-v5-{digest}.zip"
    reader_url = storage.upload(str(package), reader_key, "application/zip")
    cover_url = None
    cover_key = None
    try:
        if metadata.cover_name:
            with zipfile.ZipFile(cleaned) as archive:
                cover_name = next((n for n in archive.namelist() if n.endswith(metadata.cover_name)), None)
                if cover_name:
                    cover_path = work_dir / f"{gutenberg_id}.cover"
                    cover_path.write_bytes(archive.read(cover_name))
                    suffix = Path(cover_name).suffix.lower() or ".jpg"
                    cover_digest = hashlib.sha256(cover_path.read_bytes()).hexdigest()[:16]
                    cover_key = f"gutenberg/{gutenberg_id}/cover-{cover_digest}{suffix}"
                    # Normalize to JPEG for the public cover endpoint.
                    import fitz
                    jpeg_path = work_dir / f"{gutenberg_id}.cover.jpg"
                    pixmap = fitz.Pixmap(str(cover_path))
                    if pixmap.colorspace is None:
                        raise ValueError("Cover has no color space")
                    if pixmap.colorspace.n != 3:
                        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                    if pixmap.alpha:
                        pixmap = fitz.Pixmap(pixmap, 0)
                    jpeg_path.write_bytes(pixmap.tobytes("jpeg"))
                    cover_key = f"gutenberg/{gutenberg_id}/cover-{cover_digest}.jpg"
                    cover_url = storage.upload(str(jpeg_path), cover_key, "image/jpeg")

    except Exception:
        import logging
        logging.getLogger(__name__).warning("Cover extraction failed for Gutenberg %s", gutenberg_id, exc_info=True)
        cover_url = None
        cover_key = None

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
