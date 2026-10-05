from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sortandpost.paths import Paths
from sortandpost.posting.postiz import SPEC as PZ
from sortandpost.posting.ratelimit import RateLimiter, effective_limits
from sortandpost.posting.robopost import SPEC as RP
from sortandpost.posting.spec import RateLimit
from sortandpost.posting.store import load_channel_cache, save_channel_cache

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def test_window_counts_and_frees_up(paths: Paths) -> None:
    lim = RateLimiter(paths, PZ, {"create": RateLimit(3, 3600)})
    for i in range(3):
        assert lim.check("create", now=T0 + timedelta(minutes=i)) is None
        lim.record("create", now=T0 + timedelta(minutes=i))
    assert lim.used("create", now=T0 + timedelta(minutes=5)) == 3
    wait = lim.check("create", now=T0 + timedelta(minutes=5))
    assert wait is not None and 55 * 60 <= wait <= 55 * 60 + 1  # the first call leaves at 13:00
    assert lim.check("create", now=T0 + timedelta(minutes=60, seconds=1)) is None


def test_whole_send_budget_checked_up_front(paths: Paths) -> None:
    lim = RateLimiter(paths, PZ, {"create": RateLimit(3, 3600)})
    lim.record("create", now=T0)
    lim.record("create", now=T0 + timedelta(minutes=10))
    assert lim.check("create", 1, now=T0 + timedelta(minutes=20)) is None
    wait = lim.check("create", 2, now=T0 + timedelta(minutes=20))  # a split send: 2 creates
    assert wait is not None and wait <= 40 * 60 + 1
    assert lim.check("create", 4, now=T0) == 3600  # can never fit one window


def test_log_persists_across_instances_and_spares_other_state(paths: Paths) -> None:
    save_channel_cache(paths, PZ.key, [])
    RateLimiter(paths, PZ, {"create": RateLimit(5, 3600)}).record("create", now=T0)
    again = RateLimiter(paths, PZ, {"create": RateLimit(5, 3600)})
    assert again.used("create", now=T0 + timedelta(minutes=1)) == 1
    assert load_channel_cache(paths, PZ.key)[1] is not None  # channel cache untouched


def test_unlimited_ops_are_free(paths: Paths) -> None:
    lim = RateLimiter(paths, RP, effective_limits(RP))
    lim.record("create", now=T0)
    assert lim.check("create", 1000, now=T0) is None
    assert not paths.provider_file(RP.key).exists()  # nothing to keep


def test_overrides_from_config() -> None:
    assert effective_limits(PZ)["create"] == PZ.rate_limits["create"]
    raised = effective_limits(PZ, {"postiz": {"create": 500}})
    assert raised["create"] == RateLimit(500, PZ.rate_limits["create"].per_seconds)
    assert "create" not in effective_limits(PZ, {"postiz": {"create": 0}})
    assert effective_limits(RP, {"robopost": {"create": 10}})["create"] == RateLimit(10, 3600)
    assert effective_limits(PZ, {"robopost": {"create": 1}}) == dict(PZ.rate_limits)
