from __future__ import annotations

import html
import uuid
import zipfile
from pathlib import Path

from .metadata import DocumentMetadata


def build_epub(html_text: str, metadata: DocumentMetadata, destination: Path) -> None:
    title = html.escape(metadata.title or "Untitled")
    author = html.escape(metadata.author or "Unknown author")
    identifier = str(uuid.uuid4())
    content = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title>
<style>body{{font-family:serif;line-height:1.6;margin:5%;}}h2{{margin-top:2em;}}</style>
</head><body><h1>{title}</h1><p><em>{author}</em></p>{html_text}</body></html>"""
    container = """<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
    opf = f"""<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">urn:uuid:{identifier}</dc:identifier><dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata><manifest><item id="content" href="content.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="content"/></spine></package>"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/content.xhtml", content)
