"""Baseline for comparison: the whole knowledge pack pasted into one prompt.

No retrieval, no intent rules, no citation or number checks - the model
decides everything, including the refusal / handoff flags and the citation
locators. Scored with the same grader as the pipeline (run_eval.py --baseline).
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Dict, Optional

from . import config
from .ingest import load_documents
from .llm import LLMClient


@lru_cache(maxsize=1)
def _system_prompt() -> str:
    docs = load_documents(config.PACK_DIR)
    policy = next(d for d in docs if d.kind == "policy")
    pack = "\n\n".join(
        f"===== {d.filename} (effective {d.effective_date or 'n/a'}) =====\n{d.body}"
        for d in docs if d.kind == "knowledge"
    )
    return (
        "You are the RiverPay customer assistant. Follow this binding policy:\n\n"
        f"{policy.body}\n\n"
        "Answer ONLY from the knowledge pack below. When documents disagree, the latest effective date wins.\n\n"
        f"KNOWLEDGE PACK\n{pack}\n\n"
        'Return JSON only: {"answer": "plain language for the customer", '
        '"citations": [{"doc": "filename.md", "section": "heading"}], '
        '"refusal": false, "handoff_to_human": false, "notes": "optional"}'
    )


def answer(client: LLMClient, question_id: Optional[str], text: str) -> Dict:
    try:
        data = client.chat_json(
            [{"role": "system", "content": _system_prompt()}, {"role": "user", "content": text}],
            model=config.LLM_MODEL,
        )
    except Exception as exc:  # noqa: BLE001
        data = {"answer": "", "citations": [], "refusal": True, "handoff_to_human": True, "notes": repr(exc)}
    citations = [
        {"doc": str(c.get("doc", "")), "section": str(c.get("section", ""))}
        for c in data.get("citations") or [] if isinstance(c, dict)
    ]
    return {
        "question_id": question_id,
        "answer": str(data.get("answer", "")),
        "citations": citations,
        "refusal": bool(data.get("refusal", False)),
        "handoff_to_human": bool(data.get("handoff_to_human", False)),
        "notes": "mega-prompt baseline; " + str(data.get("notes", "")),
    }
