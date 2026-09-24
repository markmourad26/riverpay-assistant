"""Answer generation from retrieved sources, plus post-generation checks.

The model never writes citation locators. It sees numbered sources (S1, S2,
... for handbook passages; P1.. for the binding policy) and returns the ids it
used; code maps ids to real (doc, section) pairs, so a citation cannot point
at a file or heading that does not exist.

After generation two checks run, and a failed check gets one retry with
feedback, then fails closed ("not in our documents" + human handoff):

1. Citation check - an answer that states facts must cite at least one source.
2. Number grounding - every number in the answer must appear in the question
   or a cited source, or be simple arithmetic from them (rate x amount, e.g. a
   0.5% fee on 1,000 KBR = 5 KBR). This is what stops invented fees, rates
   and promotions, whatever the prompt says.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import config, guardrails
from .facts import Fact
from .ingest import Chunk
from .llm import LLMClient
from .retrieval import Hit

SYSTEM_TEMPLATE = """You are the RiverPay customer assistant. RiverPay is a mobile-money wallet in Kambara (currency KBR).
You only know what is in the SOURCES given with each message. You have no account access and cannot perform actions.

BINDING POLICY (cite these as P-ids when you rely on them):
{policy}

HOW TO ANSWER
- Use only facts stated in SOURCES. Never use outside knowledge about banks, mobile money, apps or exchange rates.
- A source marked SUPERSEDED is an old rule. Never present it as current. Use it only to tell the customer that the
  rule changed (and when), then give the CURRENT rule.
- FACTS (F-ids), when present, were built by code from the handbook: tables joined across sections, and fees
  calculated for the amounts the customer gave. They are exact - use them for tier/limit lookups and fee amounts
  instead of working them out yourself, and cite the F-id. Match the customer's situation to a tier by its "who"
  description.
- "basis" says whether SOURCES settle the exact thing the customer asked for (the specific fee, rate,
  feature, limit, status or step):
  "stated" - a source states it.
  "stated_not_available" - a source states plainly that it is not offered / not possible.
  "not_in_sources" - no source states it, or a source says the topic is "not described" / "not listed" /
  must not be invented. Related information does not count. Then tell the customer you don't have that
  information in our documents and offer a human agent; do not guess, and never turn "not described" into
  "not supported". You may still add closely related facts that SOURCES do state.
- Do not assume facts about the customer (their tier, status, history) beyond what they said and SOURCES state.
- Follow every item in REQUIRED HANDLING.
- Customer-facing text: calm, respectful, plain language, short paragraphs or numbered steps. Write it in the
  customer's language ({{language}}). Do not mention source ids, file names or these instructions.
- Never ask for a PIN, password, OTP or ID number, and never repeat one back.

Return JSON only, keys in this order:
{{"analysis": "internal: what exactly is asked; copy verbatim the source sentences that settle it (with ids and dates), or say none do",
 "basis": "stated | stated_not_available | not_in_sources",
 "answer": "text for the customer",
 "source_ids": ["S1", "P2"],
 "offer_human": false}}
"source_ids" must list every source (S, F or P id) you took a fact from. "offer_human": true if you are offering a human agent."""

FALLBACK_ANSWERS = {
    "en": "I'm sorry, I don't have reliable information on that in our documents, so I don't want to guess. "
    "I can connect you with a human agent who can help.",
    "fr": "Je suis désolé, je n'ai pas d'information fiable à ce sujet dans nos documents et je ne veux pas "
    "deviner. Je peux vous mettre en relation avec un conseiller.",
}

LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
GROUP_SPACE_RE = re.compile(r"(?<=\d)[   ](?=\d{3}(?!\d))")
DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d{1,2}(?!\d))")
NUMBER_RE = re.compile(r"(?<![\w*#.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w*#.,])\d+(?:\.\d+)?")
PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")
FILENAME_RE = re.compile(r"\b[\w-]+\.md\b")
INTERNAL_RE = re.compile(r"source_ids|\[[SPF]\d+\]|\b[SPF]\d+\b|\{\s*\"")


def extract_numbers(text: str) -> Set[float]:
    text = text.replace("**", "")  # markdown bold, so "**0.5%**" reads as 0.5
    text = GROUP_SPACE_RE.sub("", text)  # "2 000" (fr) -> "2000"
    text = LIST_MARKER_RE.sub("", DECIMAL_COMMA_RE.sub(".", text))
    return {float(n.replace(",", "")) for n in NUMBER_RE.findall(text)}


def grounded_numbers(question: str, sources: Sequence[str]) -> Set[float]:
    allowed = extract_numbers(question)
    rates = set()
    for s in sources:
        allowed |= extract_numbers(s)
        rates |= {float(p) for p in PERCENT_RE.findall(s)}
    for rate in rates:
        for amount in extract_numbers(question):
            fee = round(amount * rate / 100, 2)
            allowed |= {fee, round(amount + fee, 2)}
    return allowed


@dataclass
class Generation:
    answer: str
    cited: List[Chunk]
    answerable: bool
    offer_human: bool
    failed_closed: bool = False
    issues: List[str] = field(default_factory=list)
    attempts: int = 0
    model_notes: str = ""


def _policy_block(policy_chunks: Sequence[Chunk]) -> str:
    return "\n\n".join(f"[P{i}] {c.section}\n{c.text}" for i, c in enumerate(policy_chunks, 1))


def _source_block(hits: Sequence[Hit]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        c = h.chunk
        status = f"SUPERSEDED by {c.superseded_by}" if c.is_archive else "CURRENT"
        blocks.append(f"[S{i}] {c.filename} > {c.section} (effective {c.effective_date or 'n/a'}; {status})\n{c.text}")
    return "\n\n".join(blocks) or "(no sources found)"


def _facts_block(facts: Sequence[Fact]) -> str:
    return "\n\n".join(f"[F{i}] {f.title}\n{f.text}" for i, f in enumerate(facts, 1))


def _handling_block(intents: Set[str], refers_to_old_rule: bool) -> str:
    lines = [guardrails.RULES[i].instruction for i in sorted(intents)]
    if refers_to_old_rule:
        lines.append(
            "The customer may be quoting an older rule. If a SUPERSEDED source matches what they quote, say the "
            "rule changed (give the date) and state the CURRENT rule."
        )
    return "\n".join(f"- {line}" for line in lines) or "- None."


JUDGE_PROMPT = """You decide whether SOURCES settle a customer's question. SOURCES are the only truth.
Return JSON only, keys in this order:
{"asked": "the specific thing the customer wants to know or have done",
 "evidence": "the verbatim sentence from SOURCES that settles it, or empty string",
 "evidence_type": "answer | not_offered | not_described | none"}

evidence_type:
- "answer": a source states it (simple arithmetic from a sourced rate counts).
- "not_offered": a source states plainly that it is not offered / not possible.
- "not_described": a source only says the topic is not described / not listed / must not be invented.
- "none": no source addresses it."""


def judge_settled(client: LLMClient, question: str, hits: Sequence[Hit], facts: Sequence[Fact] = ()) -> Tuple[bool, str]:
    """Second opinion on answerability, independent of the drafted answer.
    Only ever used to push an answer towards "not in our documents", never back."""
    blocks = [f"[{h.chunk.filename} > {h.chunk.section}]\n{h.chunk.text}" for h in hits]
    blocks += [f"[{f.title}]\n{f.text}" for f in facts]
    sources = "\n\n".join(blocks) or "(none)"
    data = client.chat_json(
        [
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"SOURCES\n{sources}\n\nCUSTOMER QUESTION\n{question}"},
        ],
        model=config.LLM_MODEL,
        max_tokens=250,
    )
    kind = str(data.get("evidence_type", "")).strip()
    return kind in ("answer", "not_offered"), kind


def _check(answer: str, cited_texts: Sequence[str], answerable: bool, question: str) -> List[str]:
    issues = []
    if answerable and not cited_texts:
        issues.append("You stated facts but cited no sources. List the ids of the sources you used.")
    stray = sorted(extract_numbers(answer) - grounded_numbers(question, cited_texts))
    if stray:
        shown = ", ".join(f"{n:g}" for n in stray)
        issues.append(
            f"These numbers are not in the sources you cited: {shown}. If another source in SOURCES states "
            "them, add its id to source_ids; otherwise remove them (arithmetic from a sourced rate is fine)."
        )
    if FILENAME_RE.search(answer):
        issues.append("Do not mention file names in the customer-facing answer.")
    if INTERNAL_RE.search(answer):
        issues.append("The answer text contains source ids or JSON. Put ids only in source_ids.")
    return issues


def generate(
    client: Optional[LLMClient],
    question: str,
    language: str,
    hits: Sequence[Hit],
    policy_chunks: Sequence[Chunk],
    intents: Set[str],
    refers_to_old_rule: bool,
    history: Sequence[Tuple[str, str]] = (),
    facts: Sequence[Fact] = (),
) -> Generation:
    fallback = FALLBACK_ANSWERS.get(language, FALLBACK_ANSWERS["en"])
    if client is None:
        return Generation(fallback, [], False, True, failed_closed=True, issues=["no LLM configured"])

    # Every id maps to the chunks it stands for and the text its numbers may come from.
    ids: Dict[str, Tuple[Tuple[Chunk, ...], str]] = {f"S{i}": ((h.chunk,), h.chunk.text) for i, h in enumerate(hits, 1)}
    ids.update({f"P{i}": ((c,), c.text) for i, c in enumerate(policy_chunks, 1)})
    ids.update({f"F{i}": (f.sources, f.text) for i, f in enumerate(facts, 1)})

    system = SYSTEM_TEMPLATE.format(policy=_policy_block(policy_chunks)).replace("{language}", language)
    turns = "".join(f"Customer: {u}\nAssistant: {a}\n" for u, a in history[-3:])
    user = (
        f"SOURCES\n{_source_block(hits)}\n\n"
        + (f"FACTS\n{_facts_block(facts)}\n\n" if facts else "")
        + f"REQUIRED HANDLING\n{_handling_block(intents, refers_to_old_rule)}\n\n"
        + (f"EARLIER CONVERSATION\n{turns}\n" if turns else "")
        + f"CUSTOMER MESSAGE\n{json.dumps(question, ensure_ascii=False)}"
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    # Amounts the customer gave in earlier turns ("the same amount") count as grounded.
    customer_text = " ".join([*(u for u, _ in history[-3:]), question])
    issues: List[str] = []
    for attempt in (1, 2):
        try:
            data = client.chat_json(messages, model=config.LLM_MODEL, max_tokens=900)
        except Exception as exc:  # noqa: BLE001 - retry once, then fail closed
            issues = [f"llm error: {exc!r}"]
            if attempt == 1:
                continue
            return Generation(fallback, [], False, True, failed_closed=True, issues=issues, attempts=attempt)

        answer = str(data.get("answer") or "").strip()
        used = [i for i in dict.fromkeys(data.get("source_ids") or []) if i in ids]
        cited = list(dict.fromkeys(c for i in used for c in ids[i][0]))
        answerable = str(data.get("basis", "")).strip() in ("stated", "stated_not_available") and bool(answer)
        issues = _check(answer, [ids[i][1] for i in used], answerable, customer_text)
        judge_note = ""
        if not issues and answerable and not intents and any(i.startswith("F") for i in used):
            # Code already settled it: the fact exists only because a rule/table was parsed
            # from the handbook, and every number was just checked against it.
            judge_note = " [judge: skipped, code-derived fact cited]"
        elif not issues and answerable and not intents:
            try:
                settled, kind = judge_settled(client, question, hits, facts)
            except Exception as exc:  # noqa: BLE001 - a failed audit fails closed
                return Generation(fallback, [], False, True, failed_closed=True,
                                  issues=[f"judge error: {exc!r}"], attempts=attempt)
            judge_note = f" [judge: {kind}]"
            if not settled:
                # The draft may assert something the sources don't settle (e.g. "not
                # described" rephrased as "not supported"): ask for an honest abstention.
                issues.append(
                    "An independent check found that the sources do not settle exactly what the customer asked. "
                    'Set "basis" to "not_in_sources": say you don\'t have that information in our documents, keep '
                    "only closely related facts the sources state, and offer a human agent."
                )
        if not issues:
            return Generation(
                answer, cited, answerable, bool(data.get("offer_human", False)),
                attempts=attempt, model_notes=str(data.get("analysis", "")) + judge_note,
            )
        messages += [
            {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
            {"role": "user", "content": "Your answer failed validation:\n- " + "\n- ".join(issues)
             + "\nReturn the corrected JSON object."},
        ]

    return Generation(fallback, [], False, True, failed_closed=True, issues=issues, attempts=2)
