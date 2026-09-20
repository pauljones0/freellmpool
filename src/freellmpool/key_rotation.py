"""Numbered key slots with sticky-until-429 rotation.

A provider may configure several keys: the bare ``<PROVIDER>_API_KEY``
(slot 1) plus ``<PROVIDER>_API_KEY_2`` … ``_9``. The pool stays on its
sticky cursor slot until that key returns 429/401/403; only then does
it cool that slot down and advance. Sticky (rather than round-robin)
keeps per-key rate-limit buckets predictable and avoids spraying one
logical session across accounts.
"""

from __future__ import annotations

import threading

MAX_SLOTS = 9

# Key-slot failures worth rotating to the next slot for: rate limits and
# dead/revoked keys. Other errors describe the request or route, not the key.
ROTATE_STATUSES = frozenset({429, 401, 403})


def cool_delay(status: int, retry_after: float | None) -> float:
    """Cooldown for a failed slot: auth failures rest long, 429s honor backoff."""
    if status in (401, 403):
        return 300.0
    return max(1.0, retry_after or 60.0)


def slot_env_names(key_env: str) -> tuple[str, ...]:
    """Env var names for every slot: bare first, then ``_2`` … ``_9``."""
    return (key_env, *(f"{key_env}_{i}" for i in range(2, MAX_SLOTS + 1)))


def configured_slot_name(key_env: str, env: dict[str, str], slot: int) -> str:
    """Name of configured slot ``slot``, mirroring api_keys compaction.

    Slot indices count configured keys only, so index != suffix when slots
    are unset or blank. Out-of-range slots fall back to the base name.
    """
    names = [name for name in slot_env_names(key_env) if env.get(name)]
    if 0 <= slot < len(names):
        return names[slot]
    return key_env


class KeyRotator:
    """Per-provider sticky cursor + per-slot cooldowns (no key material).

    Thread-safe like :class:`PrefixRoutes`: the pool serves many threads and
    cursor read-modify-write races would otherwise lose updates.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cursor: dict[str, int] = {}
        self._cooled: dict[tuple[str, int], float] = {}

    def usable_slots(self, provider_id: str, count: int, now: float) -> list[int]:
        """Slot indices in try order: sticky cursor first, cooled slots skipped."""
        if count <= 0:
            return []
        with self._lock:
            start = self._cursor.get(provider_id, 0) % count
            ordered = [(start + i) % count for i in range(count)]
            return [s for s in ordered if self._cooled.get((provider_id, s), 0.0) <= now]

    def cool(self, provider_id: str, slot: int, until: float) -> None:
        with self._lock:
            self._cooled[(provider_id, slot)] = until

    def advance(self, provider_id: str, count: int) -> None:
        """Move the sticky cursor to the next slot (call only on slot failure)."""
        if count <= 0:
            return
        with self._lock:
            self._cursor[provider_id] = (self._cursor.get(provider_id, 0) + 1) % count
