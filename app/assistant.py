"""One question in, one schema-compliant answer out.

    route (LLM) + regex safety net -> intents, search queries, old-rule flag
    hybrid retrieval (archive excluded unless the customer refers to an old rule)
    structured facts for what was retrieved (tier table joined in code, fees computed in code)
    generate from numbered sources + facts -> citation + number-grounding checks
    refusal / handoff decided by guardrails.RULES in code
    handoff side effect (mocked: appended to output/handoffs.jsonl)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

from . import config, facts, guardrails, router
from .generate import generate
from .ingest import Chunk, load_chunks
from .llm import LLMClient, Usage, get_client
from .retrieval import HybridRetriever


@lru_cache(maxsize=1)
def _chunks() -> Tuple[Chunk, ...]:
    return tuple(load_chunks(config.PACK_DIR))


@lru_cache(maxsize=1)
def _retriever() -> HybridRetriever:
    return HybridRetriever(_chunks())


@lru_cache(maxsize=1)
def _client() -> Optional[LLMClient]:
    return get_client()


def usage() -> Usage:
    client = _client()
    return client.usage if client else Usage()


def warm_up() -> None:
    _retriever()


def _log_handoff(question_id: Optional[str], question: str, reasons: List[str]) -> None:
    config.HANDOFF_LOG.parent.mkdir(parents=True, exist_ok=True)
    ticket = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question_id": question_id,
        "question": question,
        "reasons": reasons,
        "status": "queued (mock - no ticketing system connected)",
    }
    with config.HANDOFF_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ticket, ensure_ascii=False) + "\n")


def answer_question(
    question_id: Optional[str],
    text: str,
    history: Sequence[Tuple[str, str]] = (),
) -> Dict:
    started = time.time()
    client = _client()

    route = router.route(client, text, history)
    regex_intents = guardrails.safety_net_intents(text)
    intents = route.intents | regex_intents
    old_rule = route.refers_to_old_rule or guardrails.mentions_old_rule(text)

    queries = [text, route.standalone_question, *route.search_queries]
    hits = _retriever().search(queries, k=config.RETRIEVAL_TOP_K, include_archive=old_rule)
    policy_chunks = [c for c in _chunks() if c.kind == "policy" and c.section != "Overview"]

    customer_texts = [text, route.standalone_question, *(u for u, _ in history[-3:])]
    kb_facts = facts.relevant_facts(facts.build(_chunks()), [h.chunk for h in hits], customer_texts)

    gen = generate(client, text, route.language, hits, policy_chunks, intents, old_rule, history, kb_facts)

    # When a policy intent fires, the rule table owns both flags (a frozen
    # wallet is guidance + handoff, not a refusal; a loan request is a refusal
    # whether or not the model calls it "answerable"). Without an intent,
    # grounding decides. A failed check always fails closed, and the model can
    # add a handoff by explicitly offering one, never remove it.
    refusal, handoff = guardrails.decide(intents)
    reasons = [i for i in sorted(intents) if guardrails.RULES[i].handoff]
    if gen.failed_closed or (not gen.answerable and not intents):
        refusal = handoff = True
        reasons.append("verification_failed" if gen.failed_closed else "not_in_documents")
    if gen.offer_human and not handoff:
        handoff = True
        reasons.append("model_offered_human")

    citations = [c.citation for c in gen.cited if not c.is_archive or old_rule]
    # An archived rule is never cited alone: pair it with the document that superseded it.
    for c in gen.cited:
        if c.is_archive and not any(x["doc"] == c.superseded_by for x in citations):
            current = next((h.chunk for h in hits if h.chunk.filename == c.superseded_by), None)
            if current:
                citations.append(current.citation)
    for intent in sorted(intents):
        doc, section = guardrails.RULES[intent].citation
        citations.append({"doc": doc, "section": section})
    if not gen.answerable and not citations:
        citations.append({"doc": guardrails.POLICY_DOC, "section": "Always"})
    citations = [dict(t) for t in dict.fromkeys(tuple(c.items()) for c in citations)]

    if handoff:
        _log_handoff(question_id, text, reasons)

    notes = (
        f"intents={sorted(intents)} (router={sorted(route.intents)}, regex={sorted(regex_intents)}"
        f"{', router_error=' + route.error if route.error else ''}); "
        f"old_rule={old_rule}; lang={route.language}; queries={queries[1:]}; "
        f"retrieved={[f'{h.chunk.filename}>{h.chunk.section} ({h.score:.3f})' for h in hits]}; "
        f"facts={[f.title.split(' (')[0] for f in kb_facts]}; "
        f"answerable={gen.answerable}; attempts={gen.attempts}; "
        f"checks={'failed: ' + ' | '.join(gen.issues) if gen.issues else 'passed'}; "
        f"handoff_reasons={reasons}; model_notes={gen.model_notes!r}; "
        f"latency_ms={(time.time() - started) * 1000:.0f}"
    )
    return {
        "question_id": question_id,
        "answer": gen.answer,
        "citations": citations,
        "refusal": refusal,
        "handoff_to_human": handoff,
        "notes": notes,
    }
