"""Per-host adaptive request pacing based on provider feedback."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import urlsplit


class AdaptiveRateLimiter:
    def __init__(
        self,
        minimum_interval: float = 0.0,
        recovery_factor: float = 0.8,
        successes_to_recover: int = 3,
        maximum_backoff: float = 60.0,
    ):
        self.minimum_interval = max(0.0, minimum_interval)
        self.current_interval = self.minimum_interval
        self.recovery_factor = min(0.99, max(0.1, recovery_factor))
        self.successes_to_recover = max(1, successes_to_recover)
        self.maximum_backoff = max(self.minimum_interval, maximum_backoff)
        self._successes = 0

    def wait(self) -> None:
        if self.current_interval > 0:
            time.sleep(self.current_interval)

    def record_success(self) -> None:
        if self.current_interval <= self.minimum_interval:
            return
        self._successes += 1
        if self._successes >= self.successes_to_recover:
            self.current_interval = max(
                self.minimum_interval,
                self.current_interval * self.recovery_factor,
            )
            self._successes = 0

    def record_throttle(
        self, retry_delay: float, retry_after: float | None = None
    ) -> float:
        self._successes = 0
        exponential = min(
            self.maximum_backoff,
            max(self.minimum_interval, self.current_interval * 2, retry_delay),
        )
        self.current_interval = max(exponential, retry_after or 0.0)
        return self.current_interval


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


_LIMITERS: dict[tuple, AdaptiveRateLimiter] = {}
_LIMITERS_LOCK = Lock()


def get_rate_limiter(url: str, config: dict) -> AdaptiveRateLimiter:
    """Get the shared limiter for a host using optional per-host floors."""
    api_config = config.get("search", {}).get("api", {})
    host = (urlsplit(url).hostname or "").lower()
    per_host = api_config.get("minimum_interval_by_host_seconds", {})
    minimum = per_host.get(host, api_config.get("rate_limit_delay_seconds", 0.0))
    recovery_factor = api_config.get("rate_limit_recovery_factor", 0.8)
    successes_to_recover = api_config.get("rate_limit_successes_to_recover", 3)
    maximum_backoff = api_config.get("maximum_backoff_seconds", 60.0)
    key = (host, float(minimum), float(recovery_factor), int(successes_to_recover), float(maximum_backoff))
    with _LIMITERS_LOCK:
        if key not in _LIMITERS:
            _LIMITERS[key] = AdaptiveRateLimiter(
                minimum_interval=key[1],
                recovery_factor=key[2],
                successes_to_recover=key[3],
                maximum_backoff=key[4],
            )
        return _LIMITERS[key]
