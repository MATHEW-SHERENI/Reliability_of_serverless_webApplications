"""M2 reliability middleware: retry policy and per-environment TTL cache.

Day 10: contract only (names, arguments, rules). The tests in
tests/unit/test_reliability.py are written against this contract first;
the implementation follows on Day 11. Standard library only.
"""
import time

MAX_ATTEMPTS = 4            # 1 attempt + at most 3 retries
BASE_MS = 100               # backoff ceiling for the first retry
CAP_MS = 1000               # backoff ceiling never exceeds this
ATTEMPT_TIMEOUT_MS = 1000   # per-attempt timeout, shortened near the deadline
MIN_REMAINING_MS = 50       # no attempt starts with less time than this left
DEADLINE_MS = 3000          # total time budget for one request
CACHE_TTL_S = 60


class Clock:
    """Real time. Tests pass a fake object with the same two methods."""

    def now(self):
        """Seconds as a float, from a clock that never jumps backwards."""
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


class RetryResult:
    """A successful call: its value and how many attempts it took."""

    def __init__(self, value, attempts):
        self.value = value
        self.attempts = attempts


def call_with_retry(call, clock, rng, deadline_ms=DEADLINE_MS):
    """Call call(timeout_s) until it succeeds, following the M2 retry rules.

    - call(timeout_s) returns a value, or raises an exception. The exception is
      retried only if its `retryable` attribute is True (5xx, timeout,
      connection error); anything else, such as a 400, is raised immediately.
    - At most MAX_ATTEMPTS attempts in total.
    - Before retry n (n = 1 for the first retry, then 2, 3) it sleeps
      rng.uniform(0, min(CAP_MS, BASE_MS * 2**n)) milliseconds, via clock.sleep.
    - Each attempt gets timeout min(ATTEMPT_TIMEOUT_MS, time left before the
      deadline), passed to call() in seconds.
    - No attempt starts with less than MIN_REMAINING_MS left, and the policy
      never sleeps past the deadline; it stops instead.

    Returns RetryResult(value, attempts) on success. On final failure it raises
    the last exception, with an `attempts` attribute set to the number of calls.
    """
    raise NotImplementedError


class TTLCache:
    """Per-execution-environment cache, used for GET /products/{id} only.

    An entry stored at time t is served while clock.now() < t + ttl_s; at or
    after that moment it is a miss.
    """

    def __init__(self, clock, ttl_s=CACHE_TTL_S):
        raise NotImplementedError

    def get(self, key):
        """Return the cached value if present and fresh, else None."""
        raise NotImplementedError

    def put(self, key, value):
        """Store value under key, stamped with the current time."""
        raise NotImplementedError
