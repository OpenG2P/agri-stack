"""Per-pod token bucket per (partner, use case).

This limits what one worker process accepts; with N pods × W workers the
effective ceiling is up to N×W times the configured rate. Global limits and
daily quotas belong to the API gateway.
"""

import threading
import time
from typing import Dict, Tuple


class RateLimiter:
    def __init__(self, clock=time.monotonic, max_keys: int = 10000):
        self._clock = clock
        self._buckets: Dict[Tuple[str, str], Tuple[float, float]] = {}  # key -> (tokens, last)
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def allow(self, partner_id: str, use_case: str, requests: int, period_seconds: int) -> bool:
        capacity = float(requests)
        refill = capacity / float(period_seconds)
        key = (partner_id, use_case)
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - last) * refill)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            if len(self._buckets) >= self._max_keys and key not in self._buckets:
                self._buckets.clear()  # bounded memory; a reset only ever allows more
            self._buckets[key] = (tokens, now)
            return allowed
