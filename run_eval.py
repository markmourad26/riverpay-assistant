"""Run a question set through the assistant and score it.

    python run_eval.py                      # 16 required questions -> output/eval_results.json + scorecard
    python run_eval.py --runs 3             # repeat to measure run-to-run stability
    python run_eval.py --set dev            # our own dev set (the only set used for tuning)
    python run_eval.py --baseline           # also run the mega-prompt baseline for comparison

The 16 required questions are held out: eval/expectations.json only grades
them. Graded runs bypass the LLM response cache unless --cache is passed.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from typing import Callable, Dict, List

from app import config
from app.ingest import load_chunks
from eval.score import failed_checks, score_item

CHECKS = ["behaviour", "cited_expected", "citations_real", "archive_paired", "facts", "clean_answer"]


def load_set(name: str):
    if name == "eval":
        questions = json.loads(config.QUESTIONS_PATH.read_text(encoding="utf-8"))
        key = json.loads((config.EVAL_DIR / "expectations.json").read_text(encoding="utf-8"))
        return [(q["question_id"], q["user"], key[q["question_id"]]) for q in questions]
    items = json.loads((config.EVAL_DIR / "dev_set.json").read_text(encoding="utf-8"))
    return [(q["id"], q["user"], q["expect"]) for q in items]


def run(label: str, answer_fn: Callable[[str, str], Dict], items, runs: int, usage_fn) -> Dict:
    chunks = load_chunks(config.PACK_DIR)
    all_runs: List[List[Dict]] = []
    latencies: List[float] = []
    u0 = usage_fn()
    for r in range(runs):
        results = []
        for qid, text, expect in items:
            started = time.time()
            result = answer_fn(qid, text)
            latencies.append((time.time() - started) * 1000)
            checks = score_item(result, expect, chunks)
            results.append({"result": result, "checks": checks})
            flags = ("REFUSAL " if result["refusal"] else "") + ("HANDOFF" if result["handoff_to_human"] else "")
            status = "PASS" if checks["pass"] else "FAIL " + ",".join(failed_checks(checks))
            print(f"[{label} run {r + 1}] {qid:>4} {flags:<16} {status:<30} {text[:55]}")
        all_runs.append(results)
    u1 = usage_fn()

    n_items = len(items)
    per_item = []
    for i, (qid, text, expect) in enumerate(items):
        runs_i = [run_results[i] for run_results in all_runs]
        per_item.append({
            "id": qid,
            "question": text,
            "passes": sum(x["checks"]["pass"] for x in runs_i),
            "refusal": [x["result"]["refusal"] for x in runs_i],
            "handoff": [x["result"]["handoff_to_human"] for x in runs_i],
            "failed_checks": sorted({c for x in runs_i for c in failed_checks(x["checks"])}),
        })
    flat = [x for run_results in all_runs for x in run_results]
    total = len(flat)
    prompt_tokens = (u1["prompt_tokens"] - u0["prompt_tokens"]) / max(total, 1)
    completion_tokens = (u1["completion_tokens"] - u0["completion_tokens"]) / max(total, 1)
    p_in, p_out = config.PRICE_PER_M_TOKENS
    cost_per_q = (prompt_tokens * p_in + completion_tokens * p_out) / 1_000_000
    return {
        "label": label,
        "runs": runs,
        "items": n_items,
        "pass_rate": sum(x["checks"]["pass"] for x in flat) / total,
        "check_rates": {c: sum(x["checks"][c] for x in flat) / total for c in CHECKS},
        "stable_items": sum(1 for p in per_item if p["passes"] in (0, runs)) / n_items,
        "latency_ms_p50": statistics.median(latencies),
        "latency_ms_p95": sorted(latencies)[int(0.95 * (len(latencies) - 1))],
        "llm_calls_per_q": (u1["calls"] - u0["calls"]) / max(total, 1),
        "json_mode_fallbacks": u1.get("json_mode_fallbacks", 0) - u0.get("json_mode_fallbacks", 0),
        "tokens_per_q": {"prompt": round(prompt_tokens), "completion": round(completion_tokens)},
        "cost_usd_per_q": cost_per_q,
        "per_item": per_item,
        "first_run": [x["result"] for x in all_runs[0]],
    }


def to_markdown(reports: List[Dict]) -> str:
    lines = []
    for rep in reports:
        lines += [
            f"## {rep['label']} - {rep['items']} questions x {rep['runs']} run(s)",
            "",
            f"- **Pass rate:** {rep['pass_rate']:.0%} (all checks per answer)",
            "- **Checks:** " + ", ".join(f"{c} {v:.0%}" for c, v in rep["check_rates"].items()),
            f"- **Stable across runs:** {rep['stable_items']:.0%} of questions",
            f"- **Latency:** p50 {rep['latency_ms_p50']:.0f} ms, p95 {rep['latency_ms_p95']:.0f} ms",
            f"- **LLM calls/question:** {rep['llm_calls_per_q']:.2f}; tokens/question: "
            f"{rep['tokens_per_q']['prompt']} in / {rep['tokens_per_q']['completion']} out; "
            f"cost ~${rep['cost_usd_per_q']:.5f}/question (${rep['cost_usd_per_q'] * 1000:.2f} per 1,000)",
            f"- **Provider JSON-mode rejections recovered by lenient parsing:** {rep['json_mode_fallbacks']}",
            "",
            "| ID | Question | refusal | handoff | passes | failed checks |",
            "|---|---|---|---|---|---|",
        ]
        for p in rep["per_item"]:
            ref = "/".join("Y" if x else "n" for x in p["refusal"])
            hand = "/".join("Y" if x else "n" for x in p["handoff"])
            q = p["question"] if len(p["question"]) <= 60 else p["question"][:57] + "..."
            lines.append(f"| {p['id']} | {q} | {ref} | {hand} | {p['passes']}/{rep['runs']} | "
                         f"{', '.join(p['failed_checks']) or '-'} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", choices=["eval", "dev"], default="eval")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--cache", action="store_true", help="reuse cached LLM responses")
    parser.add_argument("--baseline", action="store_true", help="also run the mega-prompt baseline")
    args = parser.parse_args()

    from app import assistant

    client = assistant._client()
    if client is None:
        print("No LLM_API_KEY / GROQ_API_KEY set - see README.")
        return 1
    client.use_cache = args.cache
    assistant.warm_up()
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if config.HANDOFF_LOG.exists():
        config.HANDOFF_LOG.unlink()

    items = load_set(args.set)
    usage_fn = lambda: dict(vars(client.usage))  # noqa: E731
    reports = [run("pipeline", lambda qid, t: assistant.answer_question(qid, t), items, args.runs, usage_fn)]
    if args.baseline:
        from app import megaprompt

        reports.append(run("mega-prompt baseline", lambda qid, t: megaprompt.answer(client, qid, t), items,
                           args.runs, usage_fn))

    prefix = "eval" if args.set == "eval" else "dev"
    (config.OUTPUT_DIR / f"{prefix}_results.json").write_text(
        json.dumps(reports[0]["first_run"], indent=2, ensure_ascii=False), encoding="utf-8")
    if args.baseline:
        (config.OUTPUT_DIR / f"{prefix}_baseline_results.json").write_text(
            json.dumps(reports[1]["first_run"], indent=2, ensure_ascii=False), encoding="utf-8")
    (config.OUTPUT_DIR / f"{prefix}_scorecard.json").write_text(
        json.dumps([{k: v for k, v in r.items() if k != "first_run"} for r in reports], indent=2), encoding="utf-8")
    (config.OUTPUT_DIR / f"{prefix}_scorecard.md").write_text(to_markdown(reports), encoding="utf-8")

    print()
    for rep in reports:
        print(f"{rep['label']}: pass {rep['pass_rate']:.0%} | " + ", ".join(
            f"{c} {v:.0%}" for c, v in rep["check_rates"].items()))
    print(f"Wrote output/{prefix}_results.json and output/{prefix}_scorecard.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
