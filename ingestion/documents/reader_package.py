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


def _blocks(root: _Node, chapter_id: str, content_name: str, names: set[str], assets: dict[str, str], anchors: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Extract reading blocks. When [anchors] is given it receives, for every
    element id / <a name>, the index of the first block at or after it."""
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
        if anchors is not None:
            for key in ("id", "name"):
                value = node.attrs.get(key)
                if value and value not in anchors:
                    anchors[value] = len(result)
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


# Sections shorter than this (title-page lines, links inside a printed contents
# page, bare half-title headings) are folded into the preceding section.
_MIN_SECTION_CHARS = 300
# The shallowest TOC level with at least this many entries becomes the chapter
# list; deeper entries (numbered subsections) stay inside their chapter.
_MIN_TOC_ENTRIES = 3


def _toc_entries(archive: zipfile.ZipFile, opf: str, opf_root: Any, names: set[str]) -> list[tuple[str, str | None, str]]:
    """(content file, fragment, label) for the chapter-level table-of-contents
    entries in reading order — EPUB3 nav document first, then EPUB2 toc.ncx."""
    import xml.etree.ElementTree as ET
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    items = opf_root.findall(".//opf:item", ns)
    nav = next((i for i in items if "nav" in (i.attrib.get("properties") or "").split()), None)
    ncx = next((i for i in items if i.attrib.get("media-type") == "application/x-dtbncx+xml"), None)

    def resolve(doc: str, href: str) -> tuple[str, str | None] | None:
        path, _, fragment = href.partition("#")
        name = posixpath.normpath(posixpath.join(posixpath.dirname(doc), path)) if path else doc
        return (name, fragment or None) if name in names else None

    def label(text: str) -> str:
        return _clean(re.sub(r"\s+", " ", text))

    ncx_ns = "{http://www.daisy.org/z3986/2005/ncx/}"
    entries: list[tuple[str, str | None, str, int]] = []
    try:
        if nav is not None:
            doc = posixpath.normpath(posixpath.join(posixpath.dirname(opf), nav.attrib["href"]))
            parser = _Parser()
            parser.feed(archive.read(doc).decode("utf-8", errors="replace"))
            navs = [n for n in _desc(parser.root, "nav") if "toc" in (n.attrs.get("epub:type") or n.attrs.get("role") or "")]

            def walk_nav(node: _Node, depth: int) -> None:
                for child in node.children:
                    if not isinstance(child, _Node):
                        continue
                    if child.tag == "a":
                        target = resolve(doc, child.attrs.get("href", ""))
                        if target:
                            entries.append((*target, label(_node_text(child)), depth))
                    else:
                        walk_nav(child, depth + 1 if child.tag in {"ol", "ul"} else depth)

            if navs:
                walk_nav(navs[0], -1)
        if not entries and ncx is not None:
            doc = posixpath.normpath(posixpath.join(posixpath.dirname(opf), ncx.attrib["href"]))
            nav_map = ET.fromstring(archive.read(doc)).find(ncx_ns + "navMap")

            def walk_ncx(parent: Any, depth: int) -> None:
                for point in parent.findall(ncx_ns + "navPoint"):
                    text = point.find(f"{ncx_ns}navLabel/{ncx_ns}text")
                    content = point.find(ncx_ns + "content")
                    target = resolve(doc, content.attrib.get("src", "")) if content is not None else None
                    if target and text is not None:
                        entries.append((*target, label(text.text or ""), depth))
                    walk_ncx(point, depth + 1)

            if nav_map is not None:
                walk_ncx(nav_map, 0)
    except (KeyError, ET.ParseError):
        return []
    if not entries:
        return []
    depths = sorted({depth for *_, depth in entries})
    level = next((d for d in depths if sum(1 for *_, e in entries if e <= d) >= _MIN_TOC_ENTRIES), depths[-1])
    return [(name, fragment, title) for name, fragment, title, depth in entries if depth <= level]


def _is_cover(blocks: list[dict[str, Any]]) -> bool:
    return bool(blocks) and all(b["type"] in {"image", "divider"} for b in blocks)


def _section_chars(blocks: list[dict[str, Any]]) -> int:
    return sum(len(b.get("text", "")) for b in blocks)


def _toc_sections(files: list[tuple[str, list[dict[str, Any]], dict[str, int]]], toc: list[tuple[str, str | None, str]]) -> list[tuple[str, list[dict[str, Any]]]] | None:
    """Split the book's blocks at table-of-contents targets. None when the
    TOC doesn't resolve into at least two sections (caller falls back)."""
    all_blocks: list[dict[str, Any]] = []
    file_start: dict[str, int] = {}
    file_anchors: dict[str, dict[str, int]] = {}
    for name, blocks, anchors in files:
        file_start[name] = len(all_blocks)
        file_anchors[name] = anchors
        all_blocks.extend(blocks)

    cuts: list[tuple[int, str]] = []
    for name, fragment, title in toc:
        if name not in file_start:
            continue
        offset = file_anchors[name].get(fragment, None) if fragment else 0
        if offset is None:
            continue
        position = file_start[name] + offset
        # Keep reading order; several entries on one spot collapse to the first.
        if position >= len(all_blocks) or (cuts and position <= cuts[-1][0]):
            continue
        cuts.append((position, title))
    if len(cuts) < 2:
        return None

    sections: list[tuple[str, list[dict[str, Any]]]] = []
    if cuts[0][0] > 0:
        lead = all_blocks[:cuts[0][0]]
        sections.append(("Cover" if _is_cover(lead) else cuts[0][1], lead))
    for index, (position, title) in enumerate(cuts):
        end = cuts[index + 1][0] if index + 1 < len(cuts) else len(all_blocks)
        sections.append((title or f"Section {index + 1}", all_blocks[position:end]))

    merged: list[tuple[str, list[dict[str, Any]]]] = []
    for title, blocks in sections:
        short = title != "Cover" and _section_chars(blocks) < _MIN_SECTION_CHARS
        if short and merged and merged[-1][0] != "Cover":
            merged[-1] = (merged[-1][0], merged[-1][1] + blocks)
        else:
            merged.append((title, blocks))
    return merged


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
        files: list[tuple[str, list[dict[str, Any]], dict[str, int]]] = []
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
            anchors: dict[str, int] = {}
            blocks = _blocks(parser.root, chapter_id, name, names, assets, anchors)
            files.append((name, blocks, anchors))
            total += _section_chars(blocks)

        sections = _toc_sections(files, _toc_entries(archive, opf, root, names))
        if sections is not None:
            for index, (title, blocks) in enumerate(sections):
                if not blocks:
                    continue
                chapter_id = "chapter-" + hashlib.sha1(f"{index}:{title}".encode()).hexdigest()[:12]
                for block_index, block in enumerate(blocks):
                    block["id"] = _stable(chapter_id, block_index)
                chapters.append({"id": chapter_id, "title": title, "source": "toc", "blocks": blocks})
        else:
            # No usable TOC: one chapter per spine document, as before.
            for name, blocks, _ in files:
                if not blocks:
                    continue
                chapter_id = "chapter-" + hashlib.sha1(name.encode()).hexdigest()[:12]
                fallback = "Cover" if _is_cover(blocks) else f"Chapter {len(chapters) + 1}"
                heading = next((b.get("text") for b in blocks if b["type"] == "heading"), fallback)
                chapters.append({"id": chapter_id, "title": heading, "source": name, "blocks": blocks})
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