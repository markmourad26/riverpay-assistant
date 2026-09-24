"""Interactive terminal chat for the RiverPay assistant."""
from __future__ import annotations

import json
import sys
from typing import List, Tuple

from app.assistant import answer_question, warm_up

BANNER = (
    "RiverPay customer assistant (CLI)\n"
    "Ask a question, '/json' to toggle the raw JSON, 'exit' to quit.\n"
)


def main() -> int:
    print("Loading knowledge pack and embedding model...")
    warm_up()
    print(BANNER)
    show_json = False
    history: List[Tuple[str, str]] = []
    counter = 0
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"exit", "quit"}:
            break
        if text == "/json":
            show_json = not show_json
            print(f"[json output {'on' if show_json else 'off'}]")
            continue

        counter += 1
        result = answer_question(f"CHAT{counter:02d}", text, history)
        history.append((text, result["answer"]))

        if show_json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"\nAssistant: {result['answer']}\n")
            tags = [t for t, on in (("REFUSAL", result["refusal"]), ("HANDOFF", result["handoff_to_human"])) if on]
            if tags:
                print(f"[{' | '.join(tags)}]")
            if result["citations"]:
                print("Sources: " + "; ".join(f"{c['doc']} > {c['section']}" for c in result["citations"]))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
