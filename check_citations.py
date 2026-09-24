"""Flag missing or invalid citations in a results file (default output/eval_results.json).

A citation is valid only if its (doc, section) exists in the knowledge pack.
Any answer that is not a refusal must carry at least one citation, and an
archived document may not be cited without the document that superseded it.

    python check_citations.py [path/to/results.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app import config
from app.ingest import load_chunks


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else config.OUTPUT_DIR / "eval_results.json"
    if not path.exists():
        print(f"No results at {path}. Run `python run_eval.py` first.")
        return 1

    chunks = load_chunks(config.PACK_DIR)
    real = {(c.filename, c.section) for c in chunks}
    superseded = {c.filename: c.superseded_by for c in chunks if c.is_archive}

    problems = []
    results = json.loads(path.read_text(encoding="utf-8"))
    for r in results:
        qid, cites = r.get("question_id"), r.get("citations") or []
        if not cites and not r.get("refusal"):
            problems.append(f"{qid}: answer has no citation")
        for c in cites:
            if (c.get("doc"), c.get("section")) not in real:
                problems.append(f"{qid}: citation not in pack: {c.get('doc')} > {c.get('section')}")
        docs = {c.get("doc") for c in cites}
        for d in docs & superseded.keys():
            if superseded[d] not in docs:
                problems.append(f"{qid}: cites archived {d} without {superseded[d]}")

    for p in problems:
        print("[CITATION]", p)
    print(f"{len(results)} answers checked, {len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
