from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Use the OS certificate store for outbound HTTPS (Groq, Hugging Face). Needed
# behind TLS-inspecting proxies; a no-op everywhere else.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover - optional dependency
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

PACK_DIR = ROOT_DIR / "data" / "pack"  # knowledge pack, verbatim as exported
QUESTIONS_PATH = ROOT_DIR / "data" / "questions.json"
EVAL_DIR = ROOT_DIR / "eval"
OUTPUT_DIR = ROOT_DIR / "output"
CACHE_DIR = ROOT_DIR / ".cache"
HANDOFF_LOG = OUTPUT_DIR / "handoffs.jsonl"

# Any OpenAI-compatible chat endpoint works; Groq is the default.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").strip()
LLM_API_KEY = (os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()
LLM_MODEL = (os.getenv("LLM_MODEL") or os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile").strip()
ROUTER_MODEL = os.getenv("ROUTER_MODEL", LLM_MODEL).strip()
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low").strip()  # gpt-oss models only
# Groq list price for llama-3.3-70b-versatile, USD per 1M tokens (input, output).
PRICE_PER_M_TOKENS = (
    float(os.getenv("PRICE_IN_PER_M", "0.59")),
    float(os.getenv("PRICE_OUT_PER_M", "0.79")),
)
# Disk cache of LLM responses keyed on the exact request. Makes dev iterations
# cheap and reproducible; run_eval.py disables it for the graded runs.
LLM_CACHE = os.getenv("LLM_CACHE", "1") == "1"

EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5").strip()
RETRIEVAL_TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "6"))
