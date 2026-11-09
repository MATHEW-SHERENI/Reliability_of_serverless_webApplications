"""CT3 (retry limits) and CT4 (cache TTL) from midterm Table 5.

Written before reliability.py is implemented (test-first): the expectations
come from the midterm specification, not from the implementation.

Decisions:
- Retry backoff numbering starts at n = 1.
- Cache entries expire at exactly 60 seconds: age >= TTL is a miss.

Assumed cache interface:
    cache = reliability.TTLCache(clock)
    cache.put(key, value)
    cache.get(key)
"""

import random

import pytest

import reliability
from dep_client import (
    DependencyClientError,
    DependencyServerError,
    DependencyTimeout,
)


PRODUCT = {"product_id": 42, "name": "Product 0042"}
EPS = 1e-9  # Margin for floating-point sums of seconds.


class FakeClock:
    """Time that moves only when the code sleeps or a stub pretends to work."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


HANG = object()  # Script step: a call that never answers.


class ScriptedCall:
    """A fake dependency call that plays a script, one step per attempt.

    Each step is a value to return, an exception to raise, or HANG (use up the
    whole timeout, then raise a timeout). The last step repeats if the script
    runs out. Every attempt records the timeout it was given and the time left
    before the deadline when it started.
    """

    def __init__(self, clock, *script, deadline_s=3.0):
        self.clock, self.script, self.deadline_s = clock, script, deadline_s
        self.timeouts, self.time_left = [], []

    @property
    def attempts(self):
        return len(self.timeouts)

    def __call__(self, timeout_s):
        self.timeouts.append(timeout_s)
        self.time_left.append(self.deadline_s - self.clock.now())

        step = self.script[min(self.attempts, len(self.script)) - 1]

        if step is HANG:
            self.clock.advance(timeout_s)
            raise DependencyTimeout("no answer")

        if isinstance(step, Exception):
            raise step

        return step


def server_error():
    return DependencyServerError("injected fault", status=503)


# ---------------------------------------------------------------- CT3: retries


def test_ct3_succeeds_on_third_attempt():
    clock = FakeClock()
    call = ScriptedCall(clock, server_error(), server_error(), PRODUCT)

    result = reliability.call_with_retry(call, clock, random.Random(5360))

    assert result.value == PRODUCT
    assert result.attempts == 3 == call.attempts
    assert len(clock.sleeps) == 2


def test_ct3_stops_after_four_attempts():
    clock = FakeClock()

    # The final script step repeats, so every attempt fails.
    call = ScriptedCall(clock, server_error())

    with pytest.raises(DependencyServerError) as exc:
        reliability.call_with_retry(call, clock, random.Random(5360))

    assert exc.value.attempts == 4
    assert call.attempts == 4


def test_ct3_never_retries_a_client_error():
    clock = FakeClock()
    call = ScriptedCall(
        clock,
        DependencyClientError("bad request", status=400),
    )

    with pytest.raises(DependencyClientError) as exc:
        reliability.call_with_retry(call, clock, random.Random(5360))

    assert exc.value.attempts == 1
    assert call.attempts == 1
    assert clock.sleeps == []


def test_ct3_call_that_never_returns_respects_the_deadline():
    clock = FakeClock()
    call = ScriptedCall(clock, HANG)

    with pytest.raises(DependencyTimeout):
        reliability.call_with_retry(call, clock, random.Random(5360))

    # Timeouts are retried: attempt 1 uses 1.0 s and attempt 2 ends by 2.2 s, so
    # attempt 3 always starts; its timeout is cut to the time left, ending at 3.0 s.
    assert call.attempts == 3
    assert call.timeouts[-1] < 1.0

    # The entire operation must finish within three seconds.
    assert clock.now() <= 3.0 + EPS

    # Each attempt has a positive timeout of at most one second.
    assert all(0 < timeout <= 1.0 + EPS for timeout in call.timeouts)

    # Each attempt starts with at least 50 milliseconds remaining.
    assert all(remaining >= 0.05 - EPS for remaining in call.time_left)


def test_ct3_every_sleep_is_within_its_jitter_bound():
    by_retry = {1: [], 2: [], 3: []}
    for seed in range(1000):
        clock = FakeClock()
        call = ScriptedCall(clock, server_error())

        with pytest.raises(DependencyServerError):
            reliability.call_with_retry(call, clock, random.Random(seed))

        # Four attempts have three waits between them.
        assert call.attempts == 4
        assert len(clock.sleeps) == 3

        # Decision: n starts at 1, giving bounds of 0.2, 0.4, and 0.8 seconds.
        for n, seconds in enumerate(clock.sleeps, start=1):
            upper_bound = min(1.0, 0.1 * 2**n)

            assert 0 <= seconds <= upper_bound + EPS, (
                f"seed={seed}, retry={n}, sleep={seconds}, "
                f"upper_bound={upper_bound}"
            )
            by_retry[n].append(seconds)

    # Full jitter: over 1,000 seeds the sleeps must spread over [0, bound],
    # which also pins down the decision n = 1 (n = 0 never exceeds half of it).
    for n, samples in by_retry.items():
        upper_bound = min(1.0, 0.1 * 2**n)
        assert max(samples) > 0.9 * upper_bound
        assert min(samples) < 0.1 * upper_bound


# ------------------------------------------------------------------ CT4: cache


def test_ct4_miss_before_any_put():
    clock = FakeClock()
    cache = reliability.TTLCache(clock)

    assert cache.get(42) is None


def test_ct4_hit_at_59_seconds():
    clock = FakeClock()
    cache = reliability.TTLCache(clock)

    cache.put(42, PRODUCT)
    clock.advance(59)

    assert cache.get(42) == PRODUCT


def test_ct4_miss_at_61_seconds():
    clock = FakeClock()
    cache = reliability.TTLCache(clock)

    cache.put(42, PRODUCT)
    clock.advance(61)

    assert cache.get(42) is None


def test_ct4_miss_at_exactly_60_seconds():
    clock = FakeClock()
    cache = reliability.TTLCache(clock)

    cache.put(42, PRODUCT)
    clock.advance(60)

    # Decision 2: age >= 60 seconds means the entry has expired.
    assert cache.get(42) is None
