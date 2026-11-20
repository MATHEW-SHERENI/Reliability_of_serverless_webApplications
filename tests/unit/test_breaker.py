"""Task T4: the circuit breaker and its fallback (midterm Section 3).

Completion criterion: the state transitions pass, closed -> open -> half-open ->
closed (and half-open -> open when the probe fails). Rules from the midterm: the
breaker opens after 5 consecutive failures, stays open for 15 s, then lets one
half-open probe through; the fallback is a stale cache entry or 503 with
X-Degraded.
"""
import pytest

import app
import reliability
from analysis.metrics import failure_kind, run_metrics
from dep_client import DependencyClientError
from test_app import call, get_event, order_event, server_error
# The fixtures from test_app: a fake clock (also behind app._clock and the cache),
# a fake dependency client, and the mechanism flags.
from test_app import cache_on, clock, fake, retry_on  # noqa: F401


# ------------------------------------------------- the breaker on its own


def test_closed_to_open_after_five_consecutive_failures(clock):
    breaker = reliability.CircuitBreaker(clock)
    for _ in range(4):
        assert breaker.allow()
        breaker.record_failure()
    assert breaker.state == "closed"                 # four are not enough
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()


def test_a_success_resets_the_count(clock):
    breaker = reliability.CircuitBreaker(clock)
    for _ in range(4):
        breaker.record_failure()
    breaker.record_success()
    for _ in range(4):
        breaker.record_failure()
    assert breaker.state == "closed"                 # 4 + 4, but never 5 in a row


def test_open_to_half_open_after_15_seconds_then_closed(clock):
    breaker = reliability.CircuitBreaker(clock)
    for _ in range(5):
        breaker.record_failure()
    clock.advance(14.5)
    assert not breaker.allow() and breaker.state == "open"
    clock.advance(0.5)                               # exactly 15 s after opening
    assert breaker.allow() and breaker.state == "half-open"
    assert not breaker.allow()                       # one probe at a time
    breaker.record_success()
    assert breaker.state == "closed" and breaker.allow()


def test_a_failed_probe_opens_it_for_another_cooldown(clock):
    breaker = reliability.CircuitBreaker(clock)
    for _ in range(5):
        breaker.record_failure()
    clock.advance(15)
    assert breaker.allow()
    breaker.record_failure()                         # one failed probe is enough
    assert breaker.state == "open"
    clock.advance(14)
    assert not breaker.allow()                       # the cooldown starts again
    clock.advance(1)
    assert breaker.allow() and breaker.state == "half-open"


def test_the_cache_keeps_expired_entries_for_the_fallback(clock):
    cache = reliability.TTLCache(clock)
    cache.put(42, {"version": 3})
    clock.advance(60)
    assert cache.get(42) is None                     # expired: still a miss (CT4)
    assert cache.get_stale(42) == {"version": 3}     # but kept for the breaker
    assert cache.get_stale(7) is None                # never stored


# ------------------------------------------------- the breaker in the handler

@pytest.fixture
def breaker_on(monkeypatch, fake, clock):
    """Breaker on, with a fresh breaker on the same fake clock as the cache."""
    monkeypatch.setattr(app, "MECH_BREAKER", True)
    monkeypatch.setattr(app, "_breaker", reliability.CircuitBreaker(clock))
    return app._breaker


def fail_five(fake):
    fake.script = [server_error()] * 5
    for _ in range(5):
        assert call(get_event("42"))[0] == 503


def test_open_breaker_answers_503_degraded_without_calling(fake, breaker_on):
    fail_five(fake)
    status, _, headers = call(get_event("42"))
    assert status == 503 and headers["X-Degraded"] == "true" and headers["X-Breaker"] == "open"
    assert headers["X-Attempts"] == "0" and len(fake.calls) == 5   # no sixth call


def test_open_breaker_serves_an_expired_cache_entry(fake, cache_on, breaker_on, clock):
    call(get_event("42"))                            # cached at t = 0
    clock.advance(61)                                # the entry has expired
    fail_five(fake)
    status, body, headers = call(get_event("42"))
    assert (status, headers["X-Cache"], headers["X-Degraded"]) == (200, "STALE", "true")
    assert body == {"product_id": 42, "version": 3} and headers["X-Data-Version"] == "3"
    assert len(fake.calls) == 6                      # the first read and the five failures


def test_orders_fail_fast_while_open(fake, breaker_on):
    fail_five(fake)
    status, _, headers = call(order_event({"product_id": 42, "quantity": 1}))
    assert status == 503 and headers["X-Degraded"] == "true"
    assert all(c[0] == "get_item" for c in fake.calls)   # reserve was never called


def test_probe_after_cooldown_closes_on_success(fake, breaker_on, clock):
    fail_five(fake)
    clock.advance(15)
    status, _, headers = call(get_event("42"))       # the probe; the dependency is back
    assert status == 200 and headers["X-Breaker"] == "closed"
    assert "X-Degraded" not in headers and len(fake.calls) == 6


def test_failed_probe_reopens(fake, breaker_on, clock):
    fail_five(fake)
    clock.advance(15)
    fake.script = [server_error()]
    assert call(get_event("42"))[2]["X-Breaker"] == "open"
    assert call(get_event("42"))[2]["X-Degraded"] == "true"   # fast again, no call
    assert len(fake.calls) == 6


def test_unknown_products_and_no_stock_are_answers_not_failures(fake, breaker_on):
    # 4 failures, a 404, 4 more failures: the 404 is an answer, so it resets the
    # count and the breaker never sees 5 failures in a row.
    not_found = DependencyClientError("no such item", status=404)
    fake.script = [server_error()] * 4 + [not_found] + [server_error()] * 4
    statuses = [call(get_event("42" if i != 4 else "999"))[0] for i in range(9)]
    assert statuses == [503] * 4 + [404] + [503] * 4
    assert breaker_on.state == "closed"


def test_with_retries_one_request_counts_once(fake, retry_on, breaker_on):
    fake.script = [server_error()] * 4               # one request, four failed attempts
    status, _, headers = call(get_event("42"))
    assert status == 503 and headers["X-Attempts"] == "4"
    assert breaker_on.state == "closed"              # 1 failed request, not 4


def test_configuration_names_and_breaker_off(monkeypatch, fake):
    names = {}
    for retry in (False, True):
        for cache in (False, True):
            for breaker in (False, True):
                monkeypatch.setattr(app, "MECH_RETRY", retry)
                monkeypatch.setattr(app, "MECH_CACHE", cache)
                monkeypatch.setattr(app, "MECH_BREAKER", breaker)
                names[(retry, cache, breaker)] = app.config_name()
    assert names[(False, False, True)] == "Breaker"
    assert names[(True, True, True)] == "Retry+Cache+Breaker"
    assert names[(False, True, True)] == "Cache+Breaker"
    assert names[(True, True, False)] == "Retry+Cache"
    monkeypatch.setattr(app, "MECH_BREAKER", False)
    headers = call(get_event("42"))[2]
    assert "X-Breaker" not in headers and "X-Degraded" not in headers   # Base unchanged


# ------------------------------------------------- the breaker in the metrics

def test_metrics_count_fallbacks_and_breaker_failures():
    assert failure_kind({"st": 503, "rid": "c1", "deg": "true"}, {"c1"}) == "breaker"
    assert failure_kind({"st": 503, "rid": "c1", "deg": None}, {"c1"}) == "dependency"
    versions = [{"t": 100, "product_id": 7, "version": 5}]
    records = [
        {"t": 50, "ph": "main", "op": "get", "id": 7, "st": 200, "lat": 5, "cache": "STALE",
         "ver": "4", "deg": "true", "rid": None},     # before the change: not outdated
        {"t": 150, "ph": "main", "op": "get", "id": 7, "st": 200, "lat": 5, "cache": "STALE",
         "ver": "4", "deg": "true", "rid": None},     # after it: outdated
        {"t": 160, "ph": "main", "op": "get", "id": 9, "st": 503, "lat": 2, "cache": "MISS",
         "ver": None, "deg": "true", "rid": None},    # open, nothing cached: fast 503
        {"t": 170, "ph": "main", "op": "get", "id": 9, "st": 200, "lat": 30, "cache": "HIT",
         "ver": "1", "deg": None, "rid": None},
    ]
    row, _, _ = run_metrics(records, [], versions)
    assert (row["fallback_reads"], row["fallback_outdated"], row["degraded"]) == (2, 1, 3)
    assert (row["fail_breaker"], row["failed"]) == (1, 1)
    assert row["hit_ratio"] == 1 / 2                 # HIT / (HIT + MISS); STALE is neither
