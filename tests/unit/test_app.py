import json

import pytest

import app
import reliability
from dep_client import DependencyClientError, DependencyServerError, DependencyTimeout


class FakeContext:
    aws_request_id = "req-123"


class FakeClock:
    """Time that moves only when the code sleeps or a test advances it."""
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


class FakeClient:
    """Stands in for the dependency client and records every call.

    `script` lists answers for the next calls in order (values or exceptions);
    when it is empty, every call gets `result`.
    """
    def __init__(self):
        self.result = {"product_id": 42, "version": 3}
        self.script = []
        self.calls = []

    def _respond(self, *call):
        self.calls.append(call)
        answer = self.script.pop(0) if self.script else self.result
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get_item(self, product_id, timeout_s):
        return self._respond("get_item", product_id, timeout_s)

    def reserve(self, product_id, quantity, timeout_s):
        return self._respond("reserve", product_id, quantity, timeout_s)


@pytest.fixture
def clock(monkeypatch):
    """A fake clock behind the retry sleeps and the cache, so nothing really waits."""
    fake_clock = FakeClock()
    monkeypatch.setattr(app, "_clock", fake_clock)
    monkeypatch.setattr(app, "_cache", reliability.TTLCache(fake_clock))
    return fake_clock


@pytest.fixture
def fake(monkeypatch, clock):
    """Base configuration (both flags off) with a fake dependency client."""
    client = FakeClient()
    monkeypatch.setattr(app, "_client", client)
    monkeypatch.setattr(app, "MECH_RETRY", False)
    monkeypatch.setattr(app, "MECH_CACHE", False)
    return client


@pytest.fixture
def retry_on(monkeypatch):
    monkeypatch.setattr(app, "MECH_RETRY", True)


@pytest.fixture
def cache_on(monkeypatch):
    monkeypatch.setattr(app, "MECH_CACHE", True)


def server_error():
    return DependencyServerError("injected fault", status=503)


def get_event(raw_id):
    return {"routeKey": "GET /products/{id}", "pathParameters": {"id": raw_id}}


def order_event(body):
    return {"routeKey": "POST /orders",
            "body": body if isinstance(body, str) else json.dumps(body)}


def call(event):
    response = app.handler(event, FakeContext())
    return response["statusCode"], json.loads(response["body"]), response["headers"]


def test_get_success_returns_product_and_diagnostic_headers(fake):
    status, body, headers = call(get_event("42"))
    assert status == 200 and body == {"product_id": 42, "version": 3}
    assert headers["X-Attempts"] == "1" and headers["X-Cache"] == "OFF"
    assert headers["X-Data-Version"] == "3"
    assert headers["X-Request-Id"] == "req-123" and headers["X-Env-Id"] == app.ENV_ID
    assert fake.calls == [("get_item", 42, 1.0)]


def test_cold_start_header_is_true_only_on_first_request(fake, monkeypatch):
    monkeypatch.setattr(app, "_cold", True)
    assert call(get_event("42"))[2]["X-Cold-Start"] == "true"
    assert call(get_event("42"))[2]["X-Cold-Start"] == "false"


# CT2: invalid product IDs are rejected without calling the dependency.
@pytest.mark.parametrize("raw_id", ["0", "1001", "abc", "-5", "4.2", None])
def test_ct2_invalid_product_id_returns_400(fake, raw_id):
    assert call(get_event(raw_id))[0] == 400
    assert fake.calls == []


# CT2: invalid order bodies are rejected without calling the dependency.
@pytest.mark.parametrize("body", [
    {"product_id": 42, "quantity": 0},
    {"product_id": 42, "quantity": 11},
    {"product_id": 42, "quantity": "2"},
    {"product_id": 42, "quantity": 2.0},
    {"product_id": 42, "quantity": True},
    {"product_id": 0, "quantity": 2},
    {"product_id": 42},
    "not json",
])
def test_ct2_invalid_order_returns_400(fake, body):
    assert call(order_event(body))[0] == 400
    assert fake.calls == []


def test_valid_order_calls_reserve(fake):
    fake.result = {"order_id": "abc", "product_id": 42, "quantity": 2}
    status, body, _ = call(order_event({"product_id": 42, "quantity": 2}))
    assert status == 200 and body["order_id"] == "abc"
    assert fake.calls == [("reserve", 42, 2, 1.0)]


@pytest.mark.parametrize("error, expected", [
    (DependencyClientError("not found", status=404), 404),
    (DependencyClientError("no stock", status=409), 409),
    (DependencyClientError("forbidden", status=403), 502),
    (DependencyTimeout("timed out"), 504),
    (DependencyServerError("injected fault", status=503), 503),
    (RuntimeError("bug"), 500),
])
def test_dependency_failures_map_to_status_codes(fake, error, expected):
    fake.result = error
    assert call(get_event("42"))[0] == expected


def test_unknown_route_returns_404(fake):
    assert call({"routeKey": "DELETE /products/{id}"})[0] == 404


def test_one_json_log_line_per_request(fake, capsys):
    call(get_event("42"))
    record = json.loads(capsys.readouterr().out.strip())
    assert record["type"] == "request" and record["status"] == 200
    assert record["request_id"] == "req-123" and record["attempts"] == 1


def test_log_line_names_the_configuration(fake, retry_on, cache_on, capsys):
    call(get_event("42"))
    assert json.loads(capsys.readouterr().out.strip())["config"] == "Retry+Cache"


# ------------------------------------------------------------ mechanism flags

@pytest.mark.parametrize("raw, expected", [
    ("true", True), ("false", False), ("TRUE", True), (" false ", False), (None, False),
])
def test_parse_flag_accepts_true_and_false(raw, expected):
    env = {} if raw is None else {"MECH_RETRY": raw}
    assert app.parse_flag("MECH_RETRY", env) is expected


@pytest.mark.parametrize("raw", ["yes", "1", "ture", ""])
def test_parse_flag_rejects_anything_else(raw):
    with pytest.raises(ValueError):
        app.parse_flag("MECH_RETRY", {"MECH_RETRY": raw})


# ---------------------------------------------------------------- retry (M2)

def test_retry_recovers_from_two_server_errors(fake, retry_on):
    fake.script = [server_error(), server_error()]
    status, body, headers = call(get_event("42"))
    assert status == 200 and body["product_id"] == 42
    assert headers["X-Attempts"] == "3" and headers["X-Cache"] == "OFF"
    assert len(fake.calls) == 3


def test_retry_gives_up_after_four_attempts_with_503(fake, retry_on):
    fake.result = server_error()
    status, _, headers = call(get_event("42"))
    assert status == 503 and headers["X-Attempts"] == "4"
    assert len(fake.calls) == 4


def test_retry_does_not_retry_unknown_product(fake, retry_on):
    fake.result = DependencyClientError("not found", status=404)
    status, _, headers = call(get_event("42"))
    assert status == 404 and headers["X-Attempts"] == "1"


def test_retry_timeouts_end_in_504_within_the_deadline(fake, retry_on, clock):
    fake.result = DependencyTimeout("timed out")
    status, _, headers = call(get_event("42"))
    assert status == 504 and int(headers["X-Attempts"]) >= 1
    assert clock.now() <= 3.0 + 1e-9


def test_orders_are_never_retried(fake, retry_on):
    fake.result = server_error()
    status, _, headers = call(order_event({"product_id": 42, "quantity": 2}))
    assert status == 503 and headers["X-Attempts"] == "1"
    assert len(fake.calls) == 1


# ---------------------------------------------------------------- cache (M2)

# TODO (Mathew) CT4, handler part 1: test_ct4_hit_makes_no_dependency_call
#   with cache_on: first GET 42 -> X-Cache MISS, one call;
#   second GET 42 -> X-Cache HIT, X-Attempts "0", same body and X-Data-Version,
#   and still only one call in fake.calls.

# TODO (Mathew) CT4, handler part 2: test_ct4_orders_are_never_cached
#   with cache_on: the same POST twice -> two reserve calls in fake.calls,
#   and X-Cache "OFF" on both responses.


def test_cache_entry_expires_after_60_seconds(fake, cache_on, clock):
    assert call(get_event("42"))[2]["X-Cache"] == "MISS"
    clock.advance(61)
    assert call(get_event("42"))[2]["X-Cache"] == "MISS"
    assert len(fake.calls) == 2


def test_failed_reads_are_not_cached(fake, cache_on):
    fake.script = [server_error()]
    assert call(get_event("42"))[0] == 503
    status, _, headers = call(get_event("42"))
    assert status == 200 and headers["X-Cache"] == "MISS"
    assert len(fake.calls) == 2


def test_products_are_cached_separately(fake, cache_on):
    call(get_event("42"))
    assert call(get_event("7"))[2]["X-Cache"] == "MISS"
    assert [c[1] for c in fake.calls] == [42, 7]


def test_retry_and_cache_together(fake, retry_on, cache_on):
    # The midterm's worked example: a 503, then success on the second attempt.
    fake.script = [server_error()]
    first = call(get_event("42"))[2]
    assert first["X-Cache"] == "MISS" and first["X-Attempts"] == "2"
    second = call(get_event("42"))[2]
    assert second["X-Cache"] == "HIT" and second["X-Attempts"] == "0"
    assert len(fake.calls) == 2
