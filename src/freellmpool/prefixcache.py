"""Prefix-aware agent-loop cache (G22).

An agentic loop resends its growing prefix every turn. The gateway cannot skip
sending that prefix upstream — the model needs the full context — so the honest
cache is provider-side KV reuse, orchestrated gateway-side:

* :func:`hash_prefix` fingerprints the stable prompt prefix so a follow-up turn
  of the same loop can be recognized;
* :class:`PrefixRoutes` remembers which target served a prefix, so the next
  turn lands on the already-warm target instead of spraying across the pool
  (spreading defeats provider prefix caches);
* :func:`cached_prompt_tokens` harvests provider-CONFIRMED cached-token counts
  from usage blocks. Only confirmed tokens are ever deducted from allowances;
  anything absent or malformed counts as zero, never as savings.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = ["PrefixRoutes", "cached_prompt_tokens", "hash_prefix"]


def _as_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def cached_prompt_tokens(usage: Mapping[str, Any] | None, *,
                         prompt_tokens: int | None = None) -> int:
    """Provider-confirmed cached prompt tokens in a usage block, else 0.

    Understands the OpenAI/Mistral (``prompt_tokens_details.cached_tokens``),
    Anthropic (``cache_read_input_tokens``), DeepSeek/OpenRouter
    (``prompt_cache_hit_tokens``), and Gemini
    (``cachedContentTokenCount``) shapes. The result is clamped to the
    provider's own prompt count when known, so an absurd claim can never
    manufacture a refund.
    """
    if not isinstance(usage, Mapping):
        return 0
    found: int | None = None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, Mapping):
        found = _as_count(details.get("cached_tokens"))
    if found is None:
        for key in ("cache_read_input_tokens", "prompt_cache_hit_tokens",
                    "cachedContentTokenCount"):
            found = _as_count(usage.get(key))
            if found is not None:
                break
    if found is None:
        return 0
    if prompt_tokens is None:
        prompt_tokens = _as_count(usage.get("prompt_tokens"))
        if prompt_tokens is None:
            prompt_tokens = _as_count(usage.get("promptTokenCount"))
    if prompt_tokens is not None:
        found = min(found, prompt_tokens)
    return found


def hash_prefix(messages: Sequence[Mapping[str, Any]]) -> str:
    """Stable sha256 fingerprint of a prompt prefix (a message list)."""
    payload = json.dumps(list(messages), sort_keys=True, default=str,
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PrefixRoutes:
    """Bounded FIFO map of full-request hash -> ``provider/model`` route name.

    A follow-up turn whose leading messages reproduce a remembered request is
    routed back to the same target, whose provider-side prefix cache is warm.
    Thread-safe; shared copies stay consistent under ``copy.copy``.
    """

    def __init__(self, capacity: int = 1024) -> None:
        self._capacity = max(1, capacity)
        self._lock = threading.Lock()
        self._order: list[str] = []
        self._routes: dict[str, str] = {}

    def remember(self, request_hash: str, route_name: str) -> None:
        with self._lock:
            if request_hash not in self._routes:
                self._order.append(request_hash)
            self._routes[request_hash] = route_name
            while len(self._order) > self._capacity:
                self._routes.pop(self._order.pop(0), None)

    def lookup(self, prefix_hashes: Iterable[str]) -> str | None:
        with self._lock:
            for key in prefix_hashes:
                if key in self._routes:
                    return self._routes[key]
        return None
