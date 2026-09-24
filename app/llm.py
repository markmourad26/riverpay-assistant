"""Minimal OpenAI-compatible chat client (Groq by default) with JSON mode,
retries, a disk cache, and token/cost accounting."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from . import config


class LLMError(Exception):
    pass


FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_json_object(content: str) -> Dict[str, Any]:
    """Lenient JSON-object parsing: tolerates code fences, prose around the object
    and raw control characters inside strings."""
    text = FENCE_RE.sub("", content.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"model returned no JSON object: {content[:200]!r}")
    try:
        parsed = json.loads(text[start:end + 1], strict=False)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned invalid JSON: {content[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("model returned JSON that is not an object")
    return parsed


@dataclass
class Usage:
    calls: int = 0
    cached_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    json_mode_fallbacks: int = 0

    @property
    def cost_usd(self) -> float:
        p_in, p_out = config.PRICE_PER_M_TOKENS
        return (self.prompt_tokens * p_in + self.completion_tokens * p_out) / 1_000_000

    def add(self, other: "Usage") -> None:
        for f in ("calls", "cached_calls", "prompt_tokens", "completion_tokens", "latency_ms", "json_mode_fallbacks"):
            setattr(self, f, getattr(self, f) + getattr(other, f))


@dataclass
class LLMClient:
    base_url: str
    api_key: str
    use_cache: bool = config.LLM_CACHE
    usage: Usage = field(default_factory=Usage)

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        model: str,
        max_tokens: int = 700,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if "gpt-oss" in model:
            # Reasoning model: its hidden reasoning counts against max_tokens.
            payload["reasoning_effort"] = config.REASONING_EFFORT
            payload["max_tokens"] = max_tokens + 2000
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        cache_path = config.CACHE_DIR / "llm" / f"{key}.json"
        if self.use_cache and cache_path.exists():
            self.usage.cached_calls += 1
            return json.loads(cache_path.read_text(encoding="utf-8"))

        try:
            data = self._post(payload)
        except LLMError as exc:
            # Groq's server-side JSON validation sometimes rejects output it could have
            # returned. Ask again without strict mode and parse the JSON ourselves.
            if "json_validate_failed" not in str(exc):
                raise
            self.usage.json_mode_fallbacks += 1
            data = self._post({k: v for k, v in payload.items() if k != "response_format"})
        content = data["choices"][0]["message"]["content"] or ""
        parsed = parse_json_object(content)

        u = data.get("usage", {})
        self.usage.prompt_tokens += u.get("prompt_tokens", 0)
        self.usage.completion_tokens += u.get("completion_tokens", 0)
        if self.use_cache:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
        return parsed

    def _post(self, payload: Dict[str, Any], attempts: int = 3) -> Dict[str, Any]:
        last_error: Optional[str] = None
        for attempt in range(attempts):
            started = time.time()
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=30,
                )
            except requests.RequestException as exc:
                last_error = repr(exc)
            else:
                self.usage.calls += 1
                self.usage.latency_ms += (time.time() - started) * 1000
                if resp.status_code == 200:
                    return resp.json()
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in (408, 429, 500, 502, 503, 504):
                    break
            time.sleep(1.5 * (attempt + 1))
        raise LLMError(last_error or "unknown LLM error")


def get_client() -> Optional[LLMClient]:
    if not config.LLM_API_KEY:
        return None
    return LLMClient(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
