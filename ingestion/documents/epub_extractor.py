from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .metadata import DocumentMetadata, clean_text


def extract_epub(source: Path, text_output: Path, cover_output: Path) -> DocumentMetadata:
    with zipfile.ZipFile(source) as archive:
        opf_name = next((n for n in archive.namelist() if n.lower().endswith(".opf")), None)
        if not opf_name:
            raise ValueError("EPUB has no OPF package file")
        root = ElementTree.fromstring(archive.read(opf_name))
        ns = {"dc": "http://purl.org/dc/elements/1.1/", "opf": "http://www.idpf.org/2007/opf"}

        def first(tag: str):
            node = root.find(f".//dc:{tag}", ns)
            return clean_text(node.text if node is not None else None)

        manifest = {item.attrib.get("id"): item.attrib.get("href") for item in root.findall(".//opf:item", ns)}
        spine = [item.attrib.get("idref") for item in root.findall(".//opf:itemref", ns)]
        chapters: list[str] = []
        for item_id in spine:
            href = manifest.get(item_id)
            if not href:
                continue
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_name), href))
            if name not in archive.namelist():
                continue
            raw = archive.read(name).decode("utf-8", errors="replace")
            chapters.append(raw)

        cover_name = None
        cover_id = next((m.attrib.get("content") for m in root.findall(".//opf:meta", ns) if m.attrib.get("name") == "cover"), None)
        for item in root.findall(".//opf:item", ns):
            if item.attrib.get("id") == cover_id or "cover-image" in item.attrib.get("properties", ""):
                cover_name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_name), item.attrib.get("href", "")))
                break
        cover_path = None
        if cover_name and cover_name in archive.namelist():
            cover_output.write_bytes(archive.read(cover_name))
            cover_path = str(cover_output)

    text_output.parent.mkdir(parents=True, exist_ok=True)
    text_output.write_text("\n<hr/>\n".join(chapters), encoding="utf-8")
    return DocumentMetadata(
        title=first("title"), author=first("creator"), description=first("description"),
        language=first("language"), page_count=None, chapter_count=len(chapters), cover_path=cover_path,
    )
