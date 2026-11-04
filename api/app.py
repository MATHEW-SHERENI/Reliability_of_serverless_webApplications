"""Catalog API handler (module M1).

Receives API Gateway HTTP API events for GET /products/{id} and POST /orders,
validates input, calls the dependency, maps the outcome to an HTTP status, and
adds the diagnostic headers that every measurement relies on. Day 06: one
attempt and no cache (the Base configuration); the reliability middleware (M2)
is added on Days 10-11.
"""
import base64
import json
import os
import time
import uuid

from dep_client import (DependencyClient, DependencyClientError,
                        DependencyServerError, DependencyTimeout)

ENV_ID = str(uuid.uuid4())   # identifies this execution environment
ATTEMPT_TIMEOUT_S = int(os.environ.get("ATTEMPT_TIMEOUT_MS", "1000")) / 1000
MAX_PRODUCT_ID = 1000
MAX_QUANTITY = 10

_cold = True      # True only for the first request this environment serves
_client = None    # created on first use, then reused while the environment is warm


def get_client():
    global _client
    if _client is None:
        _client = DependencyClient(os.environ["DEPENDENCY_URL"],
                                   os.environ.get("AWS_REGION", "us-east-2"))
    return _client


def parse_product_id(raw):
    """Return the product ID as an int from 1 to 1000, or None if invalid."""
    if raw is None or not raw.isdigit():
        return None
    value = int(raw)
    return value if 1 <= value <= MAX_PRODUCT_ID else None


def parse_order(body):
    """Return (product_id, quantity) from a JSON body, or None if invalid."""
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    product_id, quantity = data.get("product_id"), data.get("quantity")
    if type(product_id) is not int or type(quantity) is not int:
        return None
    if not (1 <= product_id <= MAX_PRODUCT_ID and 1 <= quantity <= MAX_QUANTITY):
        return None
    return product_id, quantity


def decode_body(event):
    body = event.get("body")
    if body and event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    return body


class Outcome:
    """What happened while serving one request; becomes the response and log line."""
    def __init__(self):
        self.status = 500
        self.body = {"error": "internal error"}
        self.attempts = 0
        self.cache = "OFF"
        self.data_version = None
        self.error_detail = None


def get_product(raw_id, outcome):
    product_id = parse_product_id(raw_id)
    if product_id is None:
        outcome.status = 400
        outcome.body = {"error": "product id must be an integer from 1 to 1000"}
        return
    outcome.attempts = 1
    item = get_client().get_item(product_id, timeout_s=ATTEMPT_TIMEOUT_S)
    outcome.status, outcome.body = 200, item
    outcome.data_version = item.get("version")


def create_order(body, outcome):
    order = parse_order(body)
    if order is None:
        outcome.status = 400
        outcome.body = {"error": "body needs integer product_id (1-1000) and quantity (1-10)"}
        return
    outcome.attempts = 1
    result = get_client().reserve(*order, timeout_s=ATTEMPT_TIMEOUT_S)
    outcome.status, outcome.body = 200, result


def handler(event, context):
    global _cold
    cold, _cold = _cold, False
    started = time.monotonic()
    outcome = Outcome()
    route = event.get("routeKey", "")

    try:
        if route == "GET /products/{id}":
            get_product((event.get("pathParameters") or {}).get("id"), outcome)
        elif route == "POST /orders":
            create_order(decode_body(event), outcome)
        else:
            outcome.status, outcome.body = 404, {"error": f"no route {route}"}
    except DependencyClientError as err:
        # 404 (unknown item) and 409 (no stock) are real answers; any other
        # 4xx means our own call was rejected, so it is our failure: 502.
        outcome.status = err.status if err.status in (404, 409) else 502
        outcome.body = {"error": str(err)}
    except DependencyTimeout as err:
        outcome.status, outcome.body = 504, {"error": str(err)}
    except DependencyServerError as err:
        outcome.status, outcome.body = 503, {"error": str(err)}
    except Exception as err:   # a bug: count it as an error, keep details in the log
        outcome.status, outcome.body = 500, {"error": "internal error"}
        outcome.error_detail = repr(err)

    latency_ms = (time.monotonic() - started) * 1000
    print(json.dumps({
        "type": "request",
        "request_id": context.aws_request_id,
        "env_id": ENV_ID,
        "cold_start": cold,
        "route": route,
        "status": outcome.status,
        "attempts": outcome.attempts,
        "cache": outcome.cache,
        "latency_ms": round(latency_ms, 2),
        "error": outcome.error_detail,
    }))

    headers = {
        "Content-Type": "application/json",
        "X-Cold-Start": str(cold).lower(),
        "X-Env-Id": ENV_ID,
        "X-Request-Id": context.aws_request_id,
        "X-Attempts": str(outcome.attempts),
        "X-Cache": outcome.cache,
    }
    if outcome.data_version is not None:
        headers["X-Data-Version"] = str(outcome.data_version)
    return {"statusCode": outcome.status, "headers": headers,
            "body": json.dumps(outcome.body)}
