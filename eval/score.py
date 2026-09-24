"""Score assistant outputs against expectations.

Per-item checks (all must pass):
- behaviour: refusal / handoff flags match (None in the key = either accepted)
- cited_expected: at least one citation from the expected docs
- citations_real: every citation is an existing (doc, section) in the pack
- archive_paired: an archived doc is never cited without its replacement
- facts: every `include` regex matches, no `exclude` regex matches
- clean_answer: no file names, source ids or JSON in the customer-facing text
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence, Set, Tuple

from app.ingest import Chunk

INTERNAL_RE = re.compile(r"\b[\w-]+\.md\b|source_ids|\[[SPF]\d+\]|\b[SPF]\d+\b|\{\s*\"")


def score_item(result: Dict, expect: Dict, chunks: Sequence[Chunk]) -> Dict[str, bool]:
    real: Set[Tuple[str, str]] = {(c.filename, c.section) for c in chunks}
    superseded = {c.filename: c.superseded_by for c in chunks if c.is_archive}
    answer = result.get("answer", "")
    cites = result.get("citations") or []
    cited_docs = {c.get("doc") for c in cites}

    checks = {
        "behaviour": all(
            expect.get(k) is None or bool(result.get(f)) == expect[k]
            for k, f in (("refusal", "refusal"), ("handoff", "handoff_to_human"))
        ),
        "cited_expected": not expect.get("cite_any") or bool(cited_docs & set(expect["cite_any"])),
        "citations_real": bool(cites) and all((c.get("doc"), c.get("section")) in real for c in cites),
        "archive_paired": all(superseded[d] in cited_docs for d in cited_docs if d in superseded),
        "facts": all(re.search(p, answer, re.I | re.M) for p in expect.get("include", []))
        and not any(re.search(p, answer, re.I | re.M) for p in expect.get("exclude", [])),
        "clean_answer": not INTERNAL_RE.search(answer),
    }
    checks["pass"] = all(checks.values())
    return checks


def failed_checks(checks: Dict[str, bool]) -> List[str]:
    return [k for k, v in checks.items() if k != "pass" and not v]
