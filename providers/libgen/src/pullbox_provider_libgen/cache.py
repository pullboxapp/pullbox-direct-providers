"""Small bounded process-local caches for LibGen source metadata."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheLookup[V]:
    hit: bool
    value: V | None = None


@dataclass(frozen=True, slots=True)
class _CacheEntry[V]:
    value: V | None
    expires_at: float


class BoundedTTLCache[K: Hashable, V]:
    """Bounded LRU cache with separate positive and negative lifetimes."""

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float,
        negative_ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1 or ttl_seconds <= 0 or negative_ttl_seconds <= 0:
            raise ValueError("cache bounds and lifetimes must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._negative_ttl_seconds = negative_ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[K, _CacheEntry[V]] = OrderedDict()

    def get(self, key: K) -> CacheLookup[V]:
        entry = self._entries.get(key)
        if entry is None:
            return CacheLookup(hit=False)
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return CacheLookup(hit=False)
        self._entries.move_to_end(key)
        return CacheLookup(hit=True, value=entry.value)

    def set(self, key: K, value: V | None) -> None:
        lifetime = self._ttl_seconds if value is not None else self._negative_ttl_seconds
        self._entries[key] = _CacheEntry(value=value, expires_at=self._clock() + lifetime)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
