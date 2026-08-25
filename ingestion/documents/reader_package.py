from __future__ import annotations

import hashlib
import html
import json
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import fitz

from .metadata import DocumentMetadata, clean_document_text

# Gutenberg boilerplate filter (fallback for passages the EPUB cleaner missed)
# ---------------------------------------------------------------------------
_GUTENBERG_BLOCK_RE = re.compile(
    r"""
    \*{3}\s*(?:START|END)\s+OF\s+(?:THE\s+)?PROJECT\s+GUTENBERG   # marker lines
    | Project\s+Gutenberg(?:-tm)?[^\n]{0,120}(?:eBook|EBook|ebook) # preamble refs
    | This\s+eBook\s+is\s+for\s+the\s+use\s+of\s+anyone             # license header
    | www\.gutenberg\.(?:org|net)                                    # URL refs
    | Most\s+people\s+start\s+at\s+our\s+Web\s+site                 # boilerplate prose
    | Produced\s+by\s+                                              # production credit
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_gutenberg_boilerplate(text: str) -> bool:
    """Return True when a cleaned block text looks like Gutenberg front/back matter."""
    return bool(_GUTENBERG_BLOCK_RE.search(text))

VERSION = "reader-v1"


class _Node:
    def __init__(self, tag: str = "root", attrs: dict[str, str] | None = None):
        self.tag = tag.lower()
        self.attrs = attrs or {}
        self.children: list[_Node | str] = []


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "iframe", "object", "svg"}:
            self.ignored += 1
            return
        if self.ignored:
            return
        node = _Node(tag, {k.lower(): v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"br", "hr", "img", "meta", "link", "input"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "iframe", "object", "svg"}:
            self.ignored = max(0, self.ignored - 1)
            return
        if self.ignored:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if not self.ignored and data.strip():
            self.stack[-1].children.append(data)


def _clean(value: str) -> str:
    return " ".join(clean_document_text(html.unescape(value)).split())


def _node_text(node: _Node) -> str:
    parts: list[str] = []
    def visit(value: _Node | str) -> None:
        if isinstance(value, str):
            parts.append(value)
        else:
            for child in value.children:
                visit(child)
            if value.tag in {"br", "p", "div", "li", "tr"}:
                parts.append(" ")
    visit(node)
    return _clean("".join(parts))


def _stable(prefix: str, index: int) -> str:
    return f"{prefix}-{index:04d}"


def _image_path(src: str, content_name: str, names: set[str]) -> str | None:
    if not src or src.startswith(("http:", "https:", "data:")):
        return None
    candidate = posixpath.normpath(posixpath.join(posixpath.dirname(content_name), src.split("#", 1)[0]))
    return candidate if candidate in names else None


def _blocks(root: _Node, chapter_id: str, content_name: str, names: set[str], assets: dict[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    counter = 0
    block_tags = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre"}

    def add(block: dict[str, Any]) -> None:
        nonlocal counter
        block["id"] = _stable(chapter_id, counter)
        counter += 1
        result.append(block)

    def walk(node: _Node) -> None:
        tag = node.tag
        if tag in {"table"}:
            rows = []
            for tr in _desc(node, "tr"):
                cells = [_node_text(cell) for cell in tr.children if isinstance(cell, _Node) and cell.tag in {"td", "th"}]
                if cells:
                    rows.append(cells)
            if rows:
                add({"type": "table", "rows": rows})
            return
        if tag == "img":
            source = _image_path(node.attrs.get("src", ""), content_name, names)
            if source:
                asset = "assets/" + hashlib.sha1(source.encode()).hexdigest()[:16] + Path(source).suffix.lower()
                assets[source] = asset
                add({"type": "image", "asset": asset, "alt": _clean(node.attrs.get("alt", ""))})
            return
        if tag == "hr":
            add({"type": "divider"})
            return
        if tag in block_tags:
            text = _node_text(node)
            if text and not _is_gutenberg_boilerplate(text):
                kind = "heading" if tag.startswith("h") else "quote" if tag == "blockquote" else "listItem" if tag == "li" else "paragraph"
                add({"type": kind, "text": text})
            return
        for child in node.children:
            if isinstance(child, _Node):
                walk(child)

    def _desc(node: _Node, wanted: str):
        found: list[_Node] = []
        for child in node.children:
            if isinstance(child, _Node):
                if child.tag == wanted:
                    found.append(child)
                found.extend(_desc(child, wanted))
        return found

    for child in root.children:
        if isinstance(child, _Node):
            walk(child)
    return result


def _desc(node: _Node, wanted: str) -> list[_Node]:
    found: list[_Node] = []
    for child in node.children:
        if isinstance(child, _Node):
            if child.tag == wanted:
                found.append(child)
            found.extend(_desc(child, wanted))
    return found


def _epub(source: Path, metadata: DocumentMetadata, destination: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        opf = next(name for name in names if name.lower().endswith(".opf"))
        import xml.etree.ElementTree as ET
        root = ET.fromstring(archive.read(opf))
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        manifest = {item.attrib.get("id"): item.attrib.get("href", "") for item in root.findall(".//opf:item", ns)}
        spine = [item.attrib.get("idref") for item in root.findall(".//opf:itemref", ns)]
        chapters: list[dict[str, Any]] = []
        assets: dict[str, str] = {}
        total = 0
        for index, ref in enumerate(spine):
            href = manifest.get(ref)
            if not href:
                continue
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf), href))
            if name not in names:
                continue
            parser = _Parser()
            parser.feed(archive.read(name).decode("utf-8", errors="replace"))
            chapter_id = "chapter-" + hashlib.sha1(name.encode()).hexdigest()[:12]
            blocks = _blocks(parser.root, chapter_id, name, names, assets)
            if not blocks:
                continue
            heading = next((b.get("text") for b in blocks if b["type"] == "heading"), f"Chapter {len(chapters) + 1}")
            chapters.append({"id": chapter_id, "title": heading, "source": name, "blocks": blocks})
            total += sum(len(b.get("text", "")) for b in blocks)
        package: dict[str, Any] = {"schema": VERSION, "title": metadata.title, "author": metadata.author, "totalCharacters": total, "chapters": [{"id": c["id"], "title": c["title"], "file": f"chapters/{c['id']}.json"} for c in chapters]}
        _write_package(destination, package, chapters, archive, assets)


def _pdf(source: Path, metadata: DocumentMetadata, destination: Path) -> None:
    chapters = []
    total = 0
    with fitz.open(source) as document:
        for page_index, page in enumerate(document):
            blocks = []
            for block_index, raw in enumerate(page.get_text("blocks")):
                text = _clean(raw[4])
                if not text:
                    continue
                kind = "heading" if len(text) < 100 and (text.isupper() or text.endswith((':', '.')) is False) else "paragraph"
                block = {"id": _stable(f"page-{page_index + 1}", block_index), "type": kind, "text": text}
                blocks.append(block)
                total += len(text)
            if blocks:
                chapter_id = f"page-{page_index + 1}"
                chapters.append({"id": chapter_id, "title": f"Page {page_index + 1}", "source": "pdf", "blocks": blocks})
    package = {"schema": VERSION, "title": metadata.title, "author": metadata.author, "totalCharacters": total, "chapters": [{"id": c["id"], "title": c["title"], "file": f"chapters/{c['id']}.json"} for c in chapters]}
    _write_package(destination, package, chapters, None, {})


def _write_package(destination: Path, manifest: dict[str, Any], chapters: list[dict[str, Any]], archive: zipfile.ZipFile | None, assets: dict[str, str]) -> None:
    chapter_ids = [chapter["id"] for chapter in chapters]
    if manifest.get("schema") != VERSION or not chapter_ids or len(set(chapter_ids)) != len(chapter_ids):
        raise ValueError("Invalid reader-v1 package: chapters must be non-empty and unique")
    if any(not chapter.get("blocks") for chapter in chapters):
        raise ValueError("Invalid reader-v1 package: empty chapter")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
        for chapter in chapters:
            output.writestr(f"chapters/{chapter['id']}.json", json.dumps(chapter, ensure_ascii=False, separators=(",", ":")))
        if archive:
            for source, target in assets.items():
                output.writestr(target, archive.read(source))
    if destination.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("reader-v1 package exceeds the 100 MB limit")


def build_reader_package(source: Path, fmt: str, metadata: DocumentMetadata, destination: Path) -> None:
    if fmt.lower() == "epub":
        _epub(source, metadata, destination)
    elif fmt.lower() == "pdf":
        _pdf(source, metadata, destination)
    else:
        raise ValueError(f"Unsupported reader package format: {fmt}")