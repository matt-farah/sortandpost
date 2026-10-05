"""Client-side rate limiting for any provider: a sliding window per
(provider, operation), counted on this machine and kept in
``local/<provider>.json`` so a restart doesn't reset it.

The limits themselves are data (``ProviderSpec.rate_limits``, overridable per
operation from ``posting.rate_limit_overrides`` in config.yaml). A send checks
its whole budget before the first call (:meth:`RateLimiter.check`), so a split
send is never stopped halfway by a limit we already knew about.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from sortandpost.paths import Paths
from sortandpost.posting.model import utcnow
from sortandpost.posting.spec import ProviderSpec, RateLimit
from sortandpost.posting.store import load_state, update_state

_KEY = "rate_log"


def effective_limits(
    spec: ProviderSpec, overrides: Mapping[str, Mapping[str, int]] | None = None
) -> dict[str, RateLimit]:
    """The spec's limits, with ``overrides[provider][op] = calls`` applied (same
    window; ``0`` turns that limit off)."""
    out = dict(spec.rate_limits)
    for op, calls in (overrides or {}).get(spec.key, {}).items():
        base = out.get(op)
        if calls <= 0:
            out.pop(op, None)
        else:
            out[op] = RateLimit(int(calls), base.per_seconds if base else 3600)
    return out


class RateLimiter:
    def __init__(self, paths: Paths, spec: ProviderSpec, limits: Mapping[str, RateLimit]) -> None:
        self._paths = paths
        self._spec = spec
        self._limits = dict(limits)

    def limit(self, op: str) -> RateLimit | None:
        return self._limits.get(op)

    def _log(self, op: str, now: datetime) -> list[datetime]:
        lim = self._limits.get(op)
        if lim is None:
            return []
        raw = load_state(self._paths, self._spec.key).get(_KEY, {})
        stamps = raw.get(op, []) if isinstance(raw, dict) else []
        cutoff = now - timedelta(seconds=lim.per_seconds)
        out: list[datetime] = []
        for s in stamps if isinstance(stamps, list) else []:
            try:
                t = datetime.fromisoformat(str(s))
            except ValueError:
                continue
            if t.tzinfo is not None and t > cutoff:
                out.append(t)
        return sorted(out)

    def used(self, op: str, *, now: datetime | None = None) -> int:
        return len(self._log(op, now or utcnow()))

    def check(self, op: str, n: int = 1, *, now: datetime | None = None) -> int | None:
        """``None`` when *n* more calls of *op* fit now; else the seconds to wait
        until they would."""
        lim = self._limits.get(op)
        if lim is None or n <= 0:
            return None
        if n > lim.calls:
            return lim.per_seconds  # never fits in one window
        now = now or utcnow()
        log = self._log(op, now)
        over = len(log) + n - lim.calls
        if over <= 0:
            return None
        # the (over)-th oldest call has to leave the window first
        frees_at = log[over - 1] + timedelta(seconds=lim.per_seconds)
        return max(1, int((frees_at - now).total_seconds()) + 1)

    def record(self, op: str, *, now: datetime | None = None) -> None:
        if op not in self._limits:
            return
        now = now or utcnow()
        log = [t.isoformat() for t in self._log(op, now)] + [now.isoformat()]
        raw = load_state(self._paths, self._spec.key).get(_KEY, {})
        merged = dict(raw) if isinstance(raw, dict) else {}
        merged[op] = log
        update_state(self._paths, self._spec.key, **{_KEY: merged})
