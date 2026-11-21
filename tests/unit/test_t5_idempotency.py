"""T5 (midterm Table 1). The order handler validates keys, resolves competing claims, retries reservations, and replays responses."""
import json
from unittest.mock import Mock

import pytest

import app
from dep_client import DependencyClientError, DependencyTimeout
from test_app import FakeClient, FakeContext, call, clock, fake, retry_on, server_error

KEY = "order-key"
BODY = {"product_id": 17, "quantity": 1}
ORDER = {"order_id": "o-example", **BODY}


class KeyedClient(FakeClient):
    """Record the key alongside each scripted reservation."""

    def reserve(self, product_id, quantity, timeout_s, idempotency_key=None):
        """Return the next scripted reservation and remember its key."""
        return self._respond("reserve", product_id, quantity, timeout_s, idempotency_key)


def _event(headers=None, body=BODY):
    return {"routeKey": "POST /orders", "headers": {"Idempotency-Key": KEY}
            if headers is None else headers, "body": json.dumps(body)}


def _enable(monkeypatch, clock):
    import idempotency as idem

    table = idem.MemoryTable()
    store = idem.IdempotencyStore(table, clock, wall_time=clock.now)
    monkeypatch.setattr(app, "_idempotency", store)
    monkeypatch.setattr(app, "MECH_IDEMPOTENCY", True)
    return store, table


@pytest.fixture
def client(fake, monkeypatch):
    """Return an isolated dependency with a successful order as its default."""
    dep = KeyedClient()
    dep.result = ORDER
    monkeypatch.setattr(app, "_client", dep)
    return dep


def test_first_request_completes_without_replay_header(monkeypatch, clock, client):
    """The winning reservation stores exactly the bytes it returns."""
    store, table = _enable(monkeypatch, clock)
    from idempotency import body_hash

    response = app.handler(_event(body={**BODY, "extra": "ignored"}), FakeContext())
    assert response["statusCode"] == 200 and json.loads(response["body"]) == ORDER
    assert "Idempotent-Replay" not in response["headers"]
    assert client.calls == [("reserve", 17, 1, 1.0, KEY)]
    outcome, record = store.begin(KEY, body_hash(BODY))
    assert outcome == "replay" and record == table.get(KEY)
    assert record["response_body"] == response["body"]


def test_ten_repeats_return_original_bytes_without_reserving(monkeypatch, clock, client):
    """All ten repeats have zero attempts and share the first raw response."""
    _enable(monkeypatch, clock)
    first = app.handler(_event(), FakeContext())
    assert first["statusCode"] == 200
    for _ in range(10):
        repeat = app.handler(_event(), FakeContext())
        assert repeat["statusCode"] == 200 and repeat["body"] == first["body"]
        assert repeat["headers"]["Idempotent-Replay"] == "true"
        assert repeat["headers"]["X-Attempts"] == "0"
    assert len(client.calls) == 1


def test_different_body_conflicts(monkeypatch, clock, client):
    """One key cannot authorize another order."""
    _enable(monkeypatch, clock)
    assert call(_event())[0] == 200
    assert call(_event(body={**BODY, "quantity": 2}))[:2] == (
        422, {"error": "idempotency key reused with a different request"})
    assert len(client.calls) == 1


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "x" * 129},
                                     {"Idempotency-Key": "two words"}],
                         ids=["missing", "long", "space"])
def test_bad_key_rejected_before_store(monkeypatch, clock, client, headers):
    """An invalid key cannot start a claim or reservation."""
    _enable(monkeypatch, clock)
    spy = Mock()
    monkeypatch.setattr(app, "_idempotency", spy)
    assert call(_event(headers))[0] == 400
    assert spy.mock_calls == [] and client.calls == []


def test_body_validation_precedes_key_validation(monkeypatch, clock, client):
    """Even without a key the original invalid-body response takes precedence."""
    event = _event({}, body={})
    _enable(monkeypatch, clock)
    spy = Mock()
    monkeypatch.setattr(app, "_idempotency", spy)
    assert call(event)[:2] == (400, {"error": "body needs integer product_id (1-1000) and quantity (1-10)"})
    assert spy.mock_calls == [] and client.calls == []


def test_header_lookup_ignores_case(monkeypatch, clock, client):
    """A mixed-case header supplies the original key."""
    _enable(monkeypatch, clock)
    assert call(_event({"iDeMpOtEnCy-KeY": KEY}))[0] == 200
    assert client.calls[0][-1] == KEY


@pytest.mark.parametrize("error, status", [
    (server_error(), 503), (DependencyClientError("stock", status=409), 409),
    (DependencyClientError("missing", status=404), 404), (DependencyTimeout("slow"), 504),
], ids=["503", "409", "404", "504"])
def test_failed_reservation_allows_fresh_request(monkeypatch, clock, client, error, status):
    """A failed claim is removed so the next request can reserve."""
    _, table = _enable(monkeypatch, clock)
    client.script = [error, ORDER]
    assert call(_event())[0] == status and table.get(KEY) is None
    code, body, headers = call(_event())
    assert code == 200 and body == ORDER and "Idempotent-Replay" not in headers
    assert [c[-1] for c in client.calls] == [KEY, KEY]


def _pending_claim(monkeypatch, clock):
    store, _ = _enable(monkeypatch, clock)
    from idempotency import body_hash

    clock.advance(20)
    deadline = clock.now() + 3
    store.begin(KEY, body_hash(BODY))
    original_begin = store.begin

    def begin(key, request_hash):
        result = original_begin(key, request_hash)
        clock.advance(1)  # Claim lookup consumes one second of the same request budget.
        return result

    monkeypatch.setattr(store, "begin", begin)
    return store, deadline


def test_busy_then_completed_replays(monkeypatch, clock, client):
    """An owner completing during the wait supplies the exact replay bytes."""
    store, _ = _pending_claim(monkeypatch, clock)
    raw = '{ "quantity": 1, "order_id":"o-example", "product_id":17 }'

    def sleep(seconds):
        clock.advance(seconds)
        store.complete(KEY, raw)

    monkeypatch.setattr(clock, "sleep", sleep)
    response = app.handler(_event(), FakeContext())
    assert response["statusCode"] == 200 and response["body"] == raw
    assert response["headers"]["Idempotent-Replay"] == "true"
    assert response["headers"]["X-Attempts"] == "0" and client.calls == []


def test_busy_then_abandoned_takes_over(monkeypatch, clock, client):
    """An abandoned claim lets the waiting request reserve once."""
    store, _ = _pending_claim(monkeypatch, clock)

    def sleep(seconds):
        clock.advance(seconds)
        store.abandon(KEY)

    monkeypatch.setattr(clock, "sleep", sleep)
    response = app.handler(_event(), FakeContext())
    assert response["statusCode"] == 200 and len(client.calls) == 1
    assert "Idempotent-Replay" not in response["headers"]


def test_busy_then_other_request_conflicts(monkeypatch, clock, client):
    """A different order completing under the key conflicts with the waiter."""
    store, _ = _pending_claim(monkeypatch, clock)
    from idempotency import body_hash

    def sleep(seconds):
        clock.advance(seconds)
        store.abandon(KEY)
        store.begin(KEY, body_hash({**BODY, "quantity": 2}))
        store.complete(KEY, "{}")

    monkeypatch.setattr(clock, "sleep", sleep)
    status, body, _ = call(_event())
    assert status == 422 and client.calls == []
    assert body == {"error": "idempotency key reused with a different request"}


def test_busy_until_deadline_is_409(monkeypatch, clock, client):
    """The three-second budget includes the initial claim lookup."""
    _, deadline = _pending_claim(monkeypatch, clock)
    status, body, _ = call(_event())
    assert status == 409 and client.calls == []
    assert body == {"error": "a request with this key is still in progress"}
    assert deadline <= clock.now() <= deadline + 0.05 + 1e-9


def test_event_without_headers_is_rejected(monkeypatch, clock, client):
    """API Gateway can omit the headers field entirely."""
    _enable(monkeypatch, clock)
    spy = Mock()
    monkeypatch.setattr(app, "_idempotency", spy)
    event = _event()
    del event["headers"]
    assert call(event)[0] == 400
    assert spy.mock_calls == [] and client.calls == []


def test_replay_needs_no_dependency_even_when_breaker_open(monkeypatch, clock, client):
    """A saved response remains available after the dependency breaker opens."""
    _enable(monkeypatch, clock)
    event = _event()
    first = app.handler(event, FakeContext())
    assert first["statusCode"] == 200 and len(client.calls) == 1
    monkeypatch.setattr(app, "MECH_BREAKER", True)
    monkeypatch.setattr(app._breaker, "allow", lambda: False)
    replay = app.handler(event, FakeContext())
    assert replay["statusCode"] == 200 and replay["body"] == first["body"]
    assert replay["headers"]["Idempotent-Replay"] == "true"
    assert len(client.calls) == 1


def test_expired_key_triggers_new_reservation(monkeypatch, clock, client):
    """The same key starts a new claim at the TTL boundary."""
    _enable(monkeypatch, clock)
    assert call(_event())[0] == 200
    clock.advance(86400)
    code, _, headers = call(_event())
    assert code == 200 and "Idempotent-Replay" not in headers and len(client.calls) == 2


def test_retry_uses_same_key_on_three_attempts(monkeypatch, clock, client, retry_on):
    """Two transient failures can recover without changing reservation identity."""
    _enable(monkeypatch, clock)
    client.script = [server_error(), server_error(), ORDER]
    code, body, headers = call(_event())
    assert code == 200 and body == ORDER and headers["X-Attempts"] == "3"
    assert [c[-1] for c in client.calls] == [KEY] * 3


def test_retry_stops_at_four_and_abandons(monkeypatch, clock, client, retry_on):
    """Exhausted retries release the claim."""
    _, table = _enable(monkeypatch, clock)
    client.result = server_error()
    code, _, headers = call(_event())
    assert code == 503 and headers["X-Attempts"] == "4"
    assert [c[-1] for c in client.calls] == [KEY] * 4 and table.get(KEY) is None


def test_open_breaker_abandons(monkeypatch, clock, client):
    """An open breaker rejects a fresh claim without a dependency attempt."""
    _, table = _enable(monkeypatch, clock)
    monkeypatch.setattr(app, "MECH_BREAKER", True)
    monkeypatch.setattr(app._breaker, "allow", lambda: False)
    assert call(_event())[0] == 503 and table.get(KEY) is None and client.calls == []


def test_flag_off_keeps_legacy_reserve_signature(fake, retry_on, monkeypatch):
    """Disabling idempotency ignores keys and prevents reservation retries."""
    monkeypatch.setattr(app, "MECH_IDEMPOTENCY", False, raising=False)
    fake.script = [server_error(), ORDER]
    for expected in (503, 200):
        code, _, headers = call(_event())
        assert code == expected and "Idempotent-Replay" not in headers
    assert fake.calls == [("reserve", 17, 1, 1.0)] * 2


def test_flag_off_reserves_twice_for_two_identical_orders(fake, monkeypatch):
    """Repeated successful requests retain the legacy unkeyed call signature."""
    monkeypatch.setattr(app, "MECH_IDEMPOTENCY", False, raising=False)
    fake.result = ORDER
    for _ in range(2):
        status, body, headers = call(_event())
        assert status == 200 and body == ORDER and "Idempotent-Replay" not in headers
    assert fake.calls == [("reserve", 17, 1, 1.0)] * 2
