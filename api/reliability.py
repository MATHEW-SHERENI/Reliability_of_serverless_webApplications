"""M2 reliability middleware: retry policy, per-environment TTL cache, and
circuit breaker.

The contract (names, arguments, rules) and the tests in
tests/unit/test_reliability.py were written first, on Day 10; this
implementation was added on Day 11 and must pass them. The circuit breaker
(task T4, midterm Section 3) was added on Day 26 with its state-transition tests
in tests/unit/test_breaker.py. Standard library only.
"""
import time

MAX_ATTEMPTS = 4            # 1 attempt + at most 3 retries
BASE_MS = 100               # base of the backoff; ceiling before retry n is BASE_MS * 2**n
CAP_MS = 1000               # backoff ceiling never exceeds this
ATTEMPT_TIMEOUT_MS = 1000   # per-attempt timeout, shortened near the deadline
MIN_REMAINING_MS = 50       # no attempt starts with less time than this left
DEADLINE_MS = 3000          # total time budget for one request
CACHE_TTL_S = 60
BREAKER_THRESHOLD = 5       # consecutive failed requests that open the breaker
BREAKER_COOLDOWN_S = 15     # how long it stays open before one half-open probe


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


def backoff_ceiling_s(n):
    """Upper bound of the sleep before retry n (n = 1, 2, 3), in seconds."""
    return min(CAP_MS, BASE_MS * 2**n) / 1000


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
    deadline = clock.now() + deadline_ms / 1000
    min_remaining = MIN_REMAINING_MS / 1000

    for attempt in range(1, MAX_ATTEMPTS + 1):
        time_left = deadline - clock.now()
        timeout_s = min(ATTEMPT_TIMEOUT_MS / 1000, time_left)
        try:
            return RetryResult(call(timeout_s), attempt)
        except Exception as err:
            err.attempts = attempt
            if not getattr(err, "retryable", False) or attempt == MAX_ATTEMPTS:
                raise
            # This was attempt n, so the next one is retry n.
            delay_s = rng.uniform(0, backoff_ceiling_s(attempt))
            if clock.now() + delay_s > deadline - min_remaining:
                raise   # after this sleep, too little time would be left
            clock.sleep(delay_s)


class TTLCache:
    """Per-execution-environment cache, used for GET /products/{id} only.

    An entry stored at time t is served while clock.now() < t + ttl_s; at or
    after that moment it is a miss. Expired entries are kept (at most one per
    product, 1,000 in all), because the circuit breaker's fallback may serve
    them while the dependency is down (get_stale).
    """

    def __init__(self, clock, ttl_s=CACHE_TTL_S):
        self._clock = clock
        self._ttl_s = ttl_s
        self._entries = {}          # key -> (value, expires_at)

    def get(self, key):
        """Return the cached value if present and fresh, else None."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._clock.now() >= expires_at:
            return None             # expired: a miss, but kept for get_stale
        return value

    def get_stale(self, key):
        """Return the stored value whatever its age, or None if never stored.

        Only the breaker's fallback uses this: an outdated price is better than
        no answer while the dependency is known to be down.
        """
        entry = self._entries.get(key)
        return None if entry is None else entry[0]

    def put(self, key, value):
        """Store value under key, stamped with the current time."""
        self._entries[key] = (value, self._clock.now() + self._ttl_s)


class CircuitBreaker:
    """Per-execution-environment circuit breaker (task T4).

    closed     calls go through; BREAKER_THRESHOLD failed requests in a row open it
               (a success resets the count)
    open       no calls for BREAKER_COOLDOWN_S; the caller serves its fallback
    half-open  after the cooldown, the next request is the one probe: success
               closes the breaker, failure opens it for another cooldown

    A failure is a request the dependency could not answer (5xx, timeout, broken
    connection), counted once per request, after any retries. Each environment
    serves one request at a time, so a probe always finishes before the next
    request asks; allow() still refuses a second request while a probe is out.
    """

    def __init__(self, clock, threshold=BREAKER_THRESHOLD, cooldown_s=BREAKER_COOLDOWN_S):
        self._clock = clock
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self.state = "closed"
        self._failures = 0
        self._opened_at = None

    def allow(self):
        """True if this request may call the dependency."""
        if self.state == "closed":
            return True
        if self.state == "open" and self._clock.now() >= self._opened_at + self._cooldown_s:
            self.state = "half-open"            # this request is the probe
            return True
        return False                            # open, or a probe is already out

    def record_success(self):
        self.state = "closed"
        self._failures = 0

    def record_failure(self):
        if self.state == "half-open":
            self._open()                        # the probe failed: another cooldown
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._open()

    def _open(self):
        self.state = "open"
        self._opened_at = self._clock.now()
        self._failures = 0
