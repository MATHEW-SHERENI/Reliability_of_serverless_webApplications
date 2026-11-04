import json

import pytest
import urllib3
from botocore.credentials import Credentials

from dep_client import (DependencyClient, DependencyClientError,
                        DependencyServerError, DependencyTimeout)

URL = "https://example.lambda-url.us-east-2.on.aws/"
FAKE_CREDS = Credentials("AKIDEXAMPLE", "SECRETEXAMPLE")


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.data = json.dumps(body).encode()


class FakeHttp:
    """Stands in for urllib3: records the request, returns or raises what we choose."""
    def __init__(self, result):
        self.result = result
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_client(result):
    http = FakeHttp(result)
    return DependencyClient(URL, "us-east-2", credentials=FAKE_CREDS, http=http), http


def test_success_returns_parsed_json_and_signs_request():
    client, http = make_client(FakeResponse(200, {"product_id": 42}))
    assert client.get_item(42, timeout_s=1.0) == {"product_id": 42}
    method, url, kwargs = http.calls[0]
    assert (method, url) == ("GET", "https://example.lambda-url.us-east-2.on.aws/items/42")
    assert kwargs["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert kwargs["timeout"].total == 1.0
    assert kwargs["retries"] is False
    assert client.last_latency_ms is not None


def test_server_error_is_retryable():
    client, _ = make_client(FakeResponse(503, {"error": "injected fault"}))
    with pytest.raises(DependencyServerError) as info:
        client.get_item(42, timeout_s=1.0)
    assert info.value.retryable and info.value.status == 503


def test_client_error_is_not_retryable():
    client, _ = make_client(FakeResponse(409, {"error": "not enough stock"}))
    with pytest.raises(DependencyClientError) as info:
        client.reserve(42, 5, timeout_s=1.0)
    assert not info.value.retryable and info.value.status == 409


def test_timeout_is_retryable():
    client, _ = make_client(urllib3.exceptions.ReadTimeoutError(None, URL, "read timed out"))
    with pytest.raises(DependencyTimeout) as info:
        client.get_item(42, timeout_s=1.0)
    assert info.value.retryable
    assert client.last_latency_ms is not None


def test_broken_connection_is_retryable_server_error():
    client, _ = make_client(urllib3.exceptions.ProtocolError("connection reset"))
    with pytest.raises(DependencyServerError) as info:
        client.get_item(42, timeout_s=1.0)
    assert info.value.retryable


def test_reserve_sends_json_body():
    client, http = make_client(FakeResponse(200, {"order_id": "abc"}))
    client.reserve(42, 2, timeout_s=1.0)
    _, url, kwargs = http.calls[0]
    assert url.endswith("/reserve")
    assert json.loads(kwargs["body"]) == {"product_id": 42, "quantity": 2}

