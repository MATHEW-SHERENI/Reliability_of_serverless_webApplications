import json

import pytest

import app
from dep_client import DependencyClientError, DependencyServerError, DependencyTimeout


class FakeContext:
    aws_request_id = "req-123"


class FakeClient:
    """Stands in for the dependency client and records every call."""
    def __init__(self):
        self.result = {"product_id": 42, "version": 3}
        self.calls = []

    def _respond(self, *call):
        self.calls.append(call)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def get_item(self, product_id, timeout_s):
        return self._respond("get_item", product_id, timeout_s)

    def reserve(self, product_id, quantity, timeout_s):
        return self._respond("reserve", product_id, quantity, timeout_s)


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(app, "_client", client)
    return client


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
