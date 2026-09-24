"""Structured facts read from the parsed handbook, and computed in code.

Two things go wrong when an LLM works from prose alone:
- joining: the tier definitions (01 > Account tiers) and the limits
  (02 > Daily send limits, 03 > Limit recap) live in different sections, so the
  model has to join "ID + selfie" -> KYC2 -> 20,000 itself, and sometimes picks
  the wrong row;
- arithmetic: fee = rate x amount, then minimum / maximum.

This module reads those tables and rules from the parsed blocks (list items and
rule statements, see ingest.Block), joins them in code and computes fees, then
hands the model ready-made facts ([F1], [F2], ...) that cite every section they
were derived from. Nothing here is hard-coded: change the handbook and the
facts change with it. Superseded documents never feed a current fact; when two
current documents disagree, the later effective date wins and the conflict is
reported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

from .ingest import Chunk

TIER_ITEM_RE = re.compile(r"^(KYC\d+)\s*[—–:-]\s*(.+)$")
AMOUNT_RE = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
LIMIT_PART_RE = re.compile(AMOUNT_RE + r"\s*(?:KBR)?\s*(?:/\s*(day|month))?", re.I)
PCT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s?%")
MIN_RE = re.compile(r"minimum\W*" + AMOUNT_RE + r"\s*KBR", re.I)
MAX_RE = re.compile(r"maximum\W*" + AMOUNT_RE + r"\s*KBR", re.I)
# Only amounts stated in the product currency count ("1,000 KBR", "15 000 KBR"), so years,
# phone numbers and tier names ("KYC2", "July 2026", "+888201002") are never priced.
CUSTOMER_AMOUNT_RE = re.compile(r"(?<![\w+*#.,])(\d{1,3}(?:[,   ]\d{3})+|\d+)(?:[.,]\d{1,2})?\s?KBR\b", re.I)


def _num(s: str) -> float:
    return float(re.sub(r"[,   ]", "", s))


def _fmt(x: float) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Fact:
    title: str
    text: str
    sources: Tuple[Chunk, ...]


@dataclass
class FeeRule:
    name: str
    chunk: Chunk
    free: bool = False
    pct: Optional[float] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    def fee(self, amount: float) -> Tuple[float, str]:
        if self.free:
            return 0.0, "free"
        raw = amount * self.pct / 100
        fee, why = raw, f"{_fmt(self.pct)}% of {_fmt(amount)} = {_fmt(raw)}"
        if self.minimum is not None and raw < self.minimum:
            fee, why = self.minimum, why + f", below the {_fmt(self.minimum)} KBR minimum"
        if self.maximum is not None and raw > self.maximum:
            fee, why = self.maximum, why + f", above the {_fmt(self.maximum)} KBR maximum"
        return round(fee, 2), why


@dataclass
class KnowledgeFacts:
    tier_fact: Optional[Fact] = None
    fee_rules: List[FeeRule] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)


def _current(chunks: Sequence[Chunk]) -> List[Chunk]:
    return [c for c in chunks if c.kind == "knowledge" and not c.is_archive]


def parse_tiers(chunks: Sequence[Chunk]) -> Tuple[Optional[Fact], List[str]]:
    definitions: Dict[str, Tuple[str, Chunk]] = {}
    limits: Dict[Tuple[str, str], List[Tuple[float, Chunk]]] = {}
    for c in _current(chunks):
        heading = c.section.lower()
        default_period = "day" if "daily" in heading or "day" in heading else "month" if "month" in heading else None
        for b in c.blocks:
            if b.kind != "list_item":
                continue
            m = TIER_ITEM_RE.match(b.plain)
            if not m:
                continue
            tier, rest = m.group(1).upper(), m.group(2).strip()
            if re.match(r"^\d", rest):  # "KYC0: 50 KBR" / "KYC1: 2,000 / day, 20,000 / month"
                for pm in LIMIT_PART_RE.finditer(rest):
                    period = pm.group(2) or default_period
                    if period:
                        limits.setdefault((tier, period.lower()), []).append((_num(pm.group(1)), c))
            else:  # "KYC2 — ID + selfie passed. Highest limits; ..."
                definitions[tier] = (rest, c)

    if not definitions and not limits:
        return None, []
    conflicts, chosen = [], {}
    for key, found in limits.items():
        values = {v for v, _ in found}
        best = max(found, key=lambda vc: vc[1].effective_date or "")
        if len(values) > 1:
            conflicts.append(f"{key[0]} {key[1]} limit differs across documents {sorted(values)}; "
                             f"using {best[1].filename} (latest effective date)")
        chosen[key] = best

    rows, sources = [], []
    for tier in sorted({t for t in definitions} | {t for t, _ in limits}):
        cells = [tier]
        if tier in definitions:
            cells.append(f"who: {definitions[tier][0]}")
            sources.append(definitions[tier][1])
        for period, label in (("day", "daily send limit"), ("month", "monthly send limit")):
            if (tier, period) in chosen:
                cells.append(f"{label}: {_fmt(chosen[(tier, period)][0])} KBR")
                sources.extend(c for _, c in limits[(tier, period)])
        rows.append("- " + " | ".join(cells))
    unique_sources = tuple(dict.fromkeys(sources))
    return Fact(
        title="Verification tiers joined with their send limits (built by code from the sections cited)",
        text="\n".join(rows),
        sources=unique_sources,
    ), conflicts


def parse_fee_rules(chunks: Sequence[Chunk]) -> List[FeeRule]:
    """A fee rule is a section whose first paragraph states it: "**0.5%** of the send
    amount, **minimum 1 KBR** ..." or "**Free** for the customer."."""
    rules = []
    for c in _current(chunks):
        para = next((b for b in c.blocks if b.kind == "paragraph"), None)
        if para is None or c.blocks[0] is not para:
            continue
        text = para.plain
        pct = PCT_RE.match(text)
        if pct:
            mn, mx = MIN_RE.search(text), MAX_RE.search(text)
            rules.append(FeeRule(c.section, c, pct=float(pct.group(1)),
                                 minimum=_num(mn.group(1)) if mn else None,
                                 maximum=_num(mx.group(1)) if mx else None))
        elif re.match(r"^free\b", text, re.I):
            rules.append(FeeRule(c.section, c, free=True))
    return rules


@lru_cache(maxsize=4)
def build(chunks: Tuple[Chunk, ...]) -> KnowledgeFacts:
    tier_fact, conflicts = parse_tiers(chunks)
    return KnowledgeFacts(tier_fact=tier_fact, fee_rules=parse_fee_rules(chunks), conflicts=conflicts)


def customer_amounts(texts: Sequence[str]) -> List[float]:
    out = []
    for t in texts:
        for m in CUSTOMER_AMOUNT_RE.finditer(t):
            v = _num(m.group(1))
            if 0 < v < 10_000_000:
                out.append(v)
    return list(dict.fromkeys(out))


def relevant_facts(kf: KnowledgeFacts, retrieved: Sequence[Chunk], customer_texts: Sequence[str]) -> List[Fact]:
    """Facts whose source sections were retrieved for this question."""
    ids = {c.chunk_id for c in retrieved}
    out: List[Fact] = []
    if kf.tier_fact and ids & {c.chunk_id for c in kf.tier_fact.sources}:
        out.append(kf.tier_fact)
    amounts = customer_amounts(customer_texts)
    for rule in kf.fee_rules:
        if rule.chunk.chunk_id not in ids or not amounts or rule.free:
            continue
        lines = []
        for a in amounts:
            fee, why = rule.fee(a)
            lines.append(f"- {_fmt(a)} KBR: fee {_fmt(fee)} KBR ({why})")
        out.append(Fact(
            title=f"Fee calculated by code for the amounts the customer gave: {rule.name} "
                  f"(effective {rule.chunk.effective_date})",
            text="\n".join(lines),
            sources=(rule.chunk,),
        ))
    return out
