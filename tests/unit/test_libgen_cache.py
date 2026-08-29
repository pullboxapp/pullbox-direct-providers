from __future__ import annotations

from pullbox_provider_libgen.cache import BoundedTTLCache


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_bounded_cache_uses_separate_positive_and_negative_ttls() -> None:
    clock = _Clock()
    cache: BoundedTTLCache[str, str] = BoundedTTLCache(
        max_entries=4,
        ttl_seconds=60,
        negative_ttl_seconds=2,
        clock=clock,
    )
    cache.set("positive", "value")
    cache.set("negative", None)

    assert cache.get("positive").value == "value"
    assert cache.get("negative").hit is True
    assert cache.get("negative").value is None

    clock.now = 3
    assert cache.get("negative").hit is False
    assert cache.get("positive").value == "value"

    clock.now = 61
    assert cache.get("positive").hit is False


def test_bounded_cache_evicts_least_recently_used_entry() -> None:
    cache: BoundedTTLCache[str, str] = BoundedTTLCache(
        max_entries=2,
        ttl_seconds=60,
        negative_ttl_seconds=2,
    )
    cache.set("first", "one")
    cache.set("second", "two")
    assert cache.get("first").hit is True

    cache.set("third", "three")

    assert cache.get("first").hit is True
    assert cache.get("second").hit is False
    assert cache.get("third").hit is True
