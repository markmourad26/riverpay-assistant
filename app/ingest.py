"""Parse the knowledge pack into a document structure, then into citable chunks.

Each file is parsed with a CommonMark parser (markdown-it) rather than regexes
over raw text, so we work with real structure: headings, paragraphs, list
items, `**Key:** value` metadata lines and `<aside>` callouts. That structure
is kept on every chunk (`Chunk.blocks`) so `facts.py` can read tables and rules
from list items and rule statements instead of guessing from free text.

Metadata comes from the documents themselves, nothing is hand-annotated:
- canonical filename: the H1 (`# 03_fees_limits.md (CURRENT)`)
- `**Document ID:**` / `**Effective:**` key-value lines, or `Effective <date>`
  inside the callout
- a callout that opens with `ARCHIVE`, or carries a `Sunset <date>`, marks an
  archived document

Conflicts are resolved at document level with the policy's rule - latest
effective date wins: documents in the same family (same numeric prefix, e.g.
`03_` and `03b_`) are compared and every older one is marked superseded by the
newest. The binding assistant policy is kept apart from the knowledge docs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark")

FILENAME_RE = re.compile(r"\S+?\.md\b")
KEY_VALUE_RE = re.compile(r"^\*\*([A-Za-z][\w ]*?):\*\*\s*(.+)$")
DATE_RE = r"(\d{4}-\d{2}-\d{2})"
FAMILY_RE = re.compile(r"^(\d+)")


def plain(md_text: str) -> str:
    """Inline markdown -> plain text (emphasis and code markers removed)."""
    return re.sub(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)|`", "", md_text).strip()


@dataclass(frozen=True)
class Block:
    kind: str  # "paragraph" | "list_item"
    text: str  # inline markdown, as written
    ordered: bool = False
    number: int = 0

    @property
    def plain(self) -> str:
        return plain(self.text)

    def render(self) -> str:
        if self.kind == "list_item":
            return f"{self.number}. {self.text}" if self.ordered else f"- {self.text}"
        return self.text


@dataclass(frozen=True)
class Document:
    filename: str
    doc_id: str
    kind: str  # "knowledge" | "policy"
    effective_date: Optional[str]
    sunset_date: Optional[str]
    superseded_by: Optional[str]
    callout: Tuple[str, ...]
    sections: Tuple[Tuple[str, Tuple[Block, ...]], ...]

    @property
    def is_archive(self) -> bool:
        return self.superseded_by is not None

    @property
    def body(self) -> str:
        return "\n\n".join(f"## {h}\n" + "\n".join(b.render() for b in blocks) for h, blocks in self.sections)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    filename: str
    section: str
    text: str
    kind: str
    effective_date: Optional[str]
    superseded_by: Optional[str]
    blocks: Tuple[Block, ...] = ()

    @property
    def is_archive(self) -> bool:
        return self.superseded_by is not None

    @property
    def citation(self) -> Dict[str, str]:
        return {"doc": self.filename, "section": self.section}

    @property
    def search_text(self) -> str:
        """What gets indexed: the heading carries much of the meaning of these short sections."""
        return f"{self.filename} > {self.section}\n{self.text}"


def parse_document(path: Path) -> Document:
    tokens = _MD.parse(path.read_text(encoding="utf-8"))

    filename: Optional[str] = None
    meta: Dict[str, str] = {}
    callout: List[str] = []
    sections: List[Tuple[str, List[Block]]] = [("Overview", [])]
    in_callout = False
    list_stack: List[List] = []  # [ordered, next_number]
    heading_level = 0
    in_list_item = 0

    for tok in tokens:
        if tok.type == "html_block":
            if "<aside" in tok.content:
                in_callout = True
            if "</aside" in tok.content:
                in_callout = False
        elif tok.type == "heading_open":
            heading_level = int(tok.tag[1])
        elif tok.type == "heading_close":
            heading_level = 0
        elif tok.type in ("bullet_list_open", "ordered_list_open"):
            start = int(tok.attrGet("start") or 1)
            list_stack.append([tok.type == "ordered_list_open", start])
        elif tok.type in ("bullet_list_close", "ordered_list_close"):
            list_stack.pop()
        elif tok.type == "list_item_open":
            in_list_item += 1
        elif tok.type == "list_item_close":
            in_list_item -= 1
            if list_stack and list_stack[-1][0]:
                list_stack[-1][1] += 1
        elif tok.type == "inline":
            content = tok.content.strip()
            if not content:
                continue
            if heading_level == 1 and filename is None:
                m = FILENAME_RE.search(content)
                filename = m.group(0) if m else path.name
            elif heading_level >= 2:
                sections.append((plain(content), []))
            elif in_callout:
                callout.append(plain(content))
            elif in_list_item and list_stack:
                ordered, number = list_stack[-1]
                sections[-1][1].append(Block("list_item", content, ordered, number))
            else:
                kv = KEY_VALUE_RE.match(content)
                if kv and len(sections) == 1:
                    meta[kv.group(1).strip()] = kv.group(2).strip()
                else:
                    sections[-1][1].append(Block("paragraph", content))

    # The callout's words ("Supersedes the ARCHIVE document ...") are part of the doc's overview.
    if callout:
        sections[0] = ("Overview", [Block("paragraph", c) for c in callout] + sections[0][1])
    callout_text = " ".join(callout)
    effective = meta.get("Effective") or next(iter(re.findall(r"Effective\W*" + DATE_RE, callout_text)), None)
    sunset = next(iter(re.findall(r"Sunset\W*" + DATE_RE, callout_text)), None)
    archived_banner = any(c.upper().startswith("ARCHIVE") for c in callout)
    first_block = next((b for _, blocks in sections for b in blocks), None)
    filename = filename or path.name

    return Document(
        filename=filename,
        doc_id=meta.get("Document ID") or Path(filename).stem,
        kind="policy" if first_block and first_block.plain.startswith("Binding.") else "knowledge",
        effective_date=re.search(DATE_RE, effective).group(1) if effective and re.search(DATE_RE, effective) else None,
        sunset_date=sunset,
        # Provisional marker for an explicit archive banner; resolved against siblings below.
        superseded_by="?" if archived_banner or sunset else None,
        callout=tuple(callout),
        sections=tuple((h, tuple(b)) for h, b in sections if b),
    )


def _resolve_supersession(docs: List[Document]) -> List[Document]:
    families: Dict[str, List[Document]] = {}
    for d in docs:
        m = FAMILY_RE.match(d.filename)
        if m and d.kind == "knowledge":
            families.setdefault(m.group(1), []).append(d)

    newest: Dict[str, str] = {}
    for family in families.values():
        dated = [d for d in family if d.effective_date]
        if len(dated) < 2:
            continue
        latest = max(dated, key=lambda d: d.effective_date)
        for d in family:
            if d is not latest:
                newest[d.filename] = latest.filename

    resolved = []
    for d in docs:
        if d.filename in newest:
            d = replace(d, superseded_by=newest[d.filename])
        elif d.superseded_by == "?":
            # Archive banner but no newer sibling found: still never current.
            d = replace(d, superseded_by="(unknown newer document)")
        resolved.append(d)
    return resolved


def load_documents(pack_dir: Path) -> List[Document]:
    docs = [parse_document(p) for p in sorted(pack_dir.glob("*.md"))]
    return _resolve_supersession(docs)


def chunk_document(doc: Document) -> List[Chunk]:
    return [
        Chunk(
            chunk_id=f"{doc.doc_id}#{i}",
            filename=doc.filename,
            section=heading,
            text="\n".join(b.render() for b in blocks),
            kind=doc.kind,
            effective_date=doc.effective_date,
            superseded_by=doc.superseded_by,
            blocks=blocks,
        )
        for i, (heading, blocks) in enumerate(doc.sections)
    ]


def load_chunks(pack_dir: Path) -> List[Chunk]:
    return [c for d in load_documents(pack_dir) for c in chunk_document(d)]
