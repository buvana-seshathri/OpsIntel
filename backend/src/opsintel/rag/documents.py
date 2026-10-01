"""Markdown documents with a small front-matter header, split into heading-aware chunks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

CORPUS_DIR = Path(__file__).parent / "corpus"


@dataclass
class ParsedDoc:
    slug: str
    title: str
    kind: str  # runbook | postmortem | policy
    services: list[str]
    acl: list[str]  # roles allowed to read it
    body: str
    content_hash: str


@dataclass
class TextChunk:
    ordinal: int
    heading: str
    content: str
    meta: dict[str, str] = field(default_factory=dict)


def parse_document(path: Path) -> ParsedDoc:
    raw = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if not match:
        raise ValueError(f"{path.name}: missing front matter")
    header: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        header[key.strip()] = value.strip()

    def as_list(value: str) -> list[str]:
        return [v.strip() for v in value.strip("[]").split(",") if v.strip()]

    missing = {"title", "kind", "acl"} - header.keys()
    if missing:
        raise ValueError(f"{path.name}: front matter missing {sorted(missing)}")
    return ParsedDoc(
        slug=path.stem,
        title=header["title"],
        kind=header["kind"],
        services=as_list(header.get("services", "")),
        acl=as_list(header["acl"]),
        body=match.group(2).strip(),
        content_hash=hashlib.sha256(raw.encode()).hexdigest(),
    )


def load_corpus(directory: Path = CORPUS_DIR) -> list[ParsedDoc]:
    return [parse_document(p) for p in sorted(directory.glob("*.md"))]


def chunk_markdown(body: str, max_chars: int = 1200) -> list[TextChunk]:
    """One chunk per heading section. Oversized sections are split on paragraph
    boundaries, carrying the previous paragraph over so no chunk starts mid-thought."""
    sections: list[tuple[str, list[str]]] = []
    heading_path: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            heading_path = heading_path[: level - 1] + [m.group(2).strip()]
            sections.append((" > ".join(heading_path), []))
        else:
            if not sections:
                sections.append(("", []))
            sections[-1][1].append(line)

    chunks: list[TextChunk] = []
    for heading, lines in sections:
        text = "\n".join(lines).strip()
        if not text:
            continue
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        current: list[str] = []
        for para in paragraphs:
            if current and len("\n\n".join([*current, para])) > max_chars:
                chunks.append(TextChunk(len(chunks), heading, "\n\n".join(current)))
                current = [current[-1]] if len(current[-1]) < max_chars // 2 else []
            current.append(para)
        if current:
            chunks.append(TextChunk(len(chunks), heading, "\n\n".join(current)))
    return chunks
