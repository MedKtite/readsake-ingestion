"""Conservative Project Gutenberg boilerplate removal for EPUBs."""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree


VERSION = "gutenberg-reader-v4"
START_RE = re.compile(
    r"\*{3}\s*START OF (?:THE )?PROJECT GUTENBERG EBOOK\b[^*<]*\*{3}",
    re.IGNORECASE,
)
END_RE = re.compile(
    r"\*{3}\s*END OF (?:THE )?PROJECT GUTENBERG EBOOK\b[^*<]*\*{3}",
    re.IGNORECASE,
)
HTML_FILES = (".html", ".htm", ".xhtml")


@dataclass(frozen=True)
class GutenbergMetadata:
    title: Optional[str]
    author: Optional[str]
    language: Optional[str]
    description: Optional[str]
    cover_name: Optional[str]
    removed_boilerplate: bool


def _text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def clean_document(text: str) -> tuple[str, bool]:
    """Keep only book content bounded by Gutenberg START/END markers.

    The surrounding document shell is preserved so the result remains valid
    XHTML. If neither marker exists, the source is returned unchanged.
    """
    start = START_RE.search(text)
    end = END_RE.search(text, start.end() if start else 0)
    if not start and not end:
        return text, False

    body_open = re.search(r"<body\b[^>]*>", text, re.IGNORECASE)
    body_close = re.search(r"</body\s*>", text, re.IGNORECASE)
    prefix = text[: body_open.end()] if body_open else ""
    suffix = text[body_close.start():] if body_close else ""
    content_start = start.end() if start else (body_open.end() if body_open else 0)
    content_end = end.start() if end else (body_close.start() if body_close else len(text))
    if content_end <= content_start:
        return text, False
    return prefix + "\n" + text[content_start:content_end].strip() + "\n" + suffix, True


# ---------------------------------------------------------------------------
# Multi-file marker tracking
# ---------------------------------------------------------------------------

def _spine_names(opf_text: str, opf_name: str, all_names: set[str]) -> list[str]:
    """Return spine HTML file names in reading order."""
    try:
        root = ElementTree.fromstring(opf_text)
    except ElementTree.ParseError:
        return []
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    manifest = {
        item.attrib.get("id"): item.attrib.get("href", "")
        for item in root.findall(".//opf:item", ns)
    }
    spine: list[str] = []
    for itemref in root.findall(".//opf:itemref", ns):
        href = manifest.get(itemref.attrib.get("idref", ""))
        if not href:
            continue
        name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_name), href))
        if name in all_names:
            spine.append(name)
    return spine


def _locate_markers(
    zin: zipfile.ZipFile, spine: list[str]
) -> tuple[Optional[tuple[str, int]], Optional[tuple[str, int]]]:
    """Scan spine files in order to find where START and END markers live.

    Returns (start_info, end_info) where each info is (filename, char_offset)
    within that file, or None if the marker was not found.
    """
    start_info: Optional[tuple[str, int]] = None
    end_info: Optional[tuple[str, int]] = None

    for name in spine:
        if not name.lower().endswith(HTML_FILES):
            continue
        try:
            text = zin.read(name).decode("utf-8", errors="replace")
        except Exception:
            continue

        if start_info is None:
            m = START_RE.search(text)
            if m:
                start_info = (name, m.end())

        if start_info is not None and end_info is None:
            search_from = start_info[1] if name == start_info[0] else 0
            m = END_RE.search(text, search_from)
            if m:
                end_info = (name, m.start())

        if start_info and end_info:
            break

    return start_info, end_info


def _metadata(opf: str, names: list[str]) -> GutenbergMetadata:
    root = ElementTree.fromstring(opf)
    ns = {"dc": "http://purl.org/dc/elements/1.1/", "opf": "http://www.idpf.org/2007/opf"}

    def first(tag: str) -> Optional[str]:
        node = root.find(f".//dc:{tag}", ns)
        return _text(node.text if node is not None else None)

    cover_id = None
    for meta in root.findall(".//opf:meta", ns):
        if meta.attrib.get("name") == "cover":
            cover_id = meta.attrib.get("content")
            break
    cover_name = None
    for item in root.findall(".//opf:item", ns):
        if item.attrib.get("id") == cover_id or "cover-image" in item.attrib.get("properties", ""):
            cover_name = item.attrib.get("href")
            break

    return GutenbergMetadata(
        title=first("title"),
        author=first("creator"),
        language=first("language"),
        description=first("description"),
        cover_name=cover_name,
        removed_boilerplate=False,
    )


def clean_epub(source: Path, destination: Path) -> GutenbergMetadata:
    """Rewrite an EPUB removing Gutenberg boilerplate, preserving all assets.

    Handles both single-file EPUBs (where START and END markers appear in the
    same HTML document) and multi-file EPUBs (where the markers appear across
    different spine documents).

    Single-file case
    ----------------
    Delegates to ``clean_document`` which strips everything outside the
    official markers while keeping the surrounding XHTML shell intact.

    Multi-file case
    ---------------
    * The file containing the START marker: everything from the start of the
      file up to the marker is replaced with just the document preamble
      (everything before ``<body>``), keeping the marker so the file remains
      self-consistent.
    * Files between the START and END files: preserved because they contain
      the book body.
    * The file containing the END marker: everything from the marker to the
      end is removed; content before the marker is kept.
    """
    with zipfile.ZipFile(source, "r") as zin:
        all_names = set(zin.namelist())
        opf_name = next((n for n in all_names if n.lower().endswith(".opf")), None)
        if not opf_name:
            raise ValueError("EPUB has no OPF package file")

        opf_text = zin.read(opf_name).decode("utf-8")
        metadata = _metadata(opf_text, list(all_names))
        spine = _spine_names(opf_text, opf_name, all_names)
        start_info, end_info = _locate_markers(zin, spine)

        changed = False
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as zout:
            for name in zin.namelist():
                data = zin.read(name)

                if name.lower().endswith(HTML_FILES):
                    text = data.decode("utf-8", errors="replace")

                    lower_name = name.lower()
                    if any(token in lower_name for token in ("pg-header", "pg-footer", "project-gutenberg-license", "license")):
                        data = b'<?xml version="1.0"?><html><body></body></html>'
                        changed = True
                    elif start_info and end_info and start_info[0] == end_info[0] == name:
                        # ── Single-file case ───────────────────────────────
                        cleaned, did_change = clean_document(text)
                        if did_change:
                            data = cleaned.encode("utf-8")
                            changed = True

                    elif start_info and name == start_info[0]:
                        # ── File that contains the START marker ────────────
                        # Drop everything *before* the marker (the preamble /
                        # title page / license header), keep content after it.
                        after = text[start_info[1]:].strip()
                        # Re-wrap in the document shell so it stays valid XHTML.
                        shell_end = re.search(r"<body\b[^>]*>", text, re.IGNORECASE)
                        prefix = text[: shell_end.end()] if shell_end else ""
                        suffix_m = re.search(r"</body\s*>", text, re.IGNORECASE)
                        suffix = text[suffix_m.start():] if suffix_m else ""
                        data = (prefix + "\n" + after + "\n" + suffix).encode("utf-8")
                        changed = True

                    elif end_info and name == end_info[0]:
                        # ── File that contains the END marker ─────────────
                        # Drop everything from the marker onwards (license).
                        before = text[: end_info[1]].rstrip()
                        shell_close_m = re.search(r"</body\s*>", text, re.IGNORECASE)
                        suffix = text[shell_close_m.start():] if shell_close_m else ""
                        data = (before + "\n" + suffix).encode("utf-8")
                        changed = True

                    else:
                        # Non-spine or no markers found — try the single-file
                        # path in case this file independently contains both.
                        cleaned, did_change = clean_document(text)
                        if did_change:
                            data = cleaned.encode("utf-8")
                            changed = True

                info = zin.getinfo(name)
                zout.writestr(info, data)

    return GutenbergMetadata(
        title=metadata.title,
        author=metadata.author,
        language=metadata.language,
        description=metadata.description,
        cover_name=metadata.cover_name,
        removed_boilerplate=changed,
    )
