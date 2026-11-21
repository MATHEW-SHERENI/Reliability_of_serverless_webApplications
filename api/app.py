"""Catalog API handler (module M1).

Receives API Gateway HTTP API events for GET /products/{id} and POST /orders,
validates input, calls the dependency, maps the outcome to an HTTP status, and
adds the diagnostic headers that every measurement relies on.

Flags, read once when the environment starts, select the configuration:
MECH_RETRY (deadline-aware retries for product reads), MECH_CACHE (a
per-environment 60 s cache for product reads), and MECH_BREAKER (task T4: a
per-environment circuit breaker in front of the dependency). All off is the
Base configuration. Orders are never cached, and never retried unless
MECH_IDEMPOTENCY is on.

MECH_IDEMPOTENCY (task T5) makes POST /orders idempotent: the Idempotency-Key
header claims a record in DynamoDB, a repeat of the same request gets the
first response back unchanged (Idempotent-Replay: true) without a dependency
call, and the dependency derives the order ID from the key, so even a retried
reservation cannot create a second order. Only then are orders retried.

While the breaker is open, no dependency call is made: a product read is served
from the cache even if its entry has expired (X-Cache: STALE), or fails at once
with 503; an order fails at once with 503. Both carry X-Degraded: true.
"""
import base64
import json
import os
import random
import time
import uuid

import idempotency
import reliability
from dep_client import (DependencyClient, DependencyClientError,
                        DependencyServerError, DependencyTimeout)

ENV_ID = str(uuid.uuid4())   # identifies this execution environment
RUN_ID = os.environ.get("RUN_ID", "none")   # set by the orchestrator for each run
ATTEMPT_TIMEOUT_S = int(os.environ.get("ATTEMPT_TIMEOUT_MS", "1000")) / 1000
MAX_PRODUCT_ID = 1000
MAX_QUANTITY = 10


def parse_flag(name, env=os.environ):
    """Read a true/false flag; anything else stops the environment from starting."""
    value = env.get(name, "false").strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be 'true' or 'false', not {value!r}")
    return value == "true"


MECH_RETRY = parse_flag("MECH_RETRY")
MECH_CACHE = parse_flag("MECH_CACHE")
MECH_BREAKER = parse_flag("MECH_BREAKER")
MECH_IDEMPOTENCY = parse_flag("MECH_IDEMPOTENCY")
CONFIG_NAMES = {(False, False): "Base", (True, False): "Retry",
                (False, True): "Cache", (True, True): "Retry+Cache"}

_clock = reliability.Clock()
_rng = random.Random()                    # jitter: unseeded, differs per environment
_cache = reliability.TTLCache(_clock)     # lives as long as this environment
_breaker = reliability.CircuitBreaker(_clock)   # so does the breaker's state

_cold = True      # True only for the first request this environment serves
_client = None    # created on first use, then reused while the environment is warm
_idempotency = None   # the T5 store, created on first use like the client


def get_client():
    global _client
    if _client is None:
        _client = DependencyClient(os.environ["DEPENDENCY_URL"],
                                   os.environ.get("AWS_REGION", "us-east-2"))
    return _client


def get_idempotency():
    global _idempotency
    if _idempotency is None:
        table = idempotency.DynamoTable(os.environ["IDEMPOTENCY_TABLE"])
        _idempotency = idempotency.IdempotencyStore(table, _clock)
    return _idempotency


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
        self.degraded = False
        self.raw_body = None        # T5: the exact response text, sent unchanged
        self.idempotency = None     # T5: new, replay, conflict, or busy


class BreakerOpen(Exception):
    """The breaker is open and there is nothing to fall back on: answer 503 at once."""


def config_name():
    name = CONFIG_NAMES[(MECH_RETRY, MECH_CACHE)]
    if MECH_BREAKER:
        name = "Breaker" if name == "Base" else name + "+Breaker"
    return name


def through_breaker(call):
    """Run call() if the breaker allows it, and tell the breaker how it went.

    Raises BreakerOpen without calling when the breaker is open. Only failures
    of the dependency itself count (5xx, timeout, broken connection: the
    retryable errors); a 404 or 409 is an answer, so it counts as a success.
    """
    if not MECH_BREAKER:
        return call()
    if not _breaker.allow():
        raise BreakerOpen()
    try:
        result = call()
    except DependencyClientError as err:
        if err.status in (404, 409):
            _breaker.record_success()           # the dependency answered
        raise
    except Exception as err:
        if getattr(err, "retryable", False):
            _breaker.record_failure()
        raise
    _breaker.record_success()
    return result


def fetch_product(product_id, outcome):
    """Read one product from the dependency: one attempt, or the retry policy."""
    def attempt(timeout_s):
        return get_client().get_item(product_id, timeout_s=timeout_s)

    if not MECH_RETRY:
        outcome.attempts = 1
        return attempt(ATTEMPT_TIMEOUT_S)
    try:
        result = reliability.call_with_retry(attempt, _clock, _rng)
    except Exception as err:
        outcome.attempts = getattr(err, "attempts", 1)
        raise                             # the status mapping in handler() still applies
    outcome.attempts = result.attempts
    return result.value


def get_product(raw_id, outcome):
    product_id = parse_product_id(raw_id)
    if product_id is None:
        outcome.status = 400
        outcome.body = {"error": "product id must be an integer from 1 to 1000"}
        return
    if MECH_CACHE:
        item = _cache.get(product_id)
        if item is not None:              # hit: no dependency call, attempts stays 0
            outcome.cache = "HIT"
            outcome.status, outcome.body = 200, item
            outcome.data_version = item.get("version")
            return
        outcome.cache = "MISS"
    try:
        item = through_breaker(lambda: fetch_product(product_id, outcome))
    except BreakerOpen:
        stale = _cache.get_stale(product_id) if MECH_CACHE else None
        outcome.degraded = True
        if stale is None:
            raise                                 # nothing to fall back on: 503
        outcome.cache = "STALE"                   # expired entry, served on purpose
        outcome.status, outcome.body = 200, stale
        outcome.data_version = stale.get("version")
        return
    if MECH_CACHE:
        _cache.put(product_id, item)      # only successful reads are stored
    outcome.status, outcome.body = 200, item
    outcome.data_version = item.get("version")


def place_order(order, outcome, key=None):
    """Reserve stock through the breaker. Without a key: one attempt, never
    retried, since a retry could place the order twice. With a key (T5): the
    dependency deduplicates by key, so the retry policy may be used."""
    if key is None:
        def reserve():
            outcome.attempts = 1
            return get_client().reserve(*order, timeout_s=ATTEMPT_TIMEOUT_S)
    else:
        def attempt(timeout_s):
            return get_client().reserve(*order, timeout_s=timeout_s, idempotency_key=key)

        def reserve():
            if not MECH_RETRY:
                outcome.attempts = 1
                return attempt(ATTEMPT_TIMEOUT_S)
            try:
                result = reliability.call_with_retry(attempt, _clock, _rng)
            except Exception as err:
                outcome.attempts = getattr(err, "attempts", 1)
                raise
            outcome.attempts = result.attempts
            return result.value
    try:
        return through_breaker(reserve)
    except BreakerOpen:
        outcome.degraded = True                   # orders have no fallback: 503 at once
        raise


def header(headers, name):
    """A request header by name, in any letter case (HTTP names are case-insensitive)."""
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def create_order(body, outcome, headers=None):
    order = parse_order(body)
    if order is None:
        outcome.status = 400
        outcome.body = {"error": "body needs integer product_id (1-1000) and quantity (1-10)"}
        return
    if not MECH_IDEMPOTENCY:
        outcome.status, outcome.body = 200, place_order(order, outcome)
        return
    create_order_once(order, outcome, header(headers, "Idempotency-Key"))


def create_order_once(order, outcome, key):
    """T5: place the order once per key; repeats get the first response back."""
    started = _clock.now()                        # the 3 s budget counts from here
    if not idempotency.valid_key(key):
        outcome.status = 400
        outcome.body = {"error": "Idempotency-Key header must be 1-128 printable "
                                 "ASCII characters without spaces"}
        return
    store = get_idempotency()
    request_hash = idempotency.body_hash({"product_id": order[0], "quantity": order[1]})
    state, record = store.begin(key, request_hash)
    if state == "busy":
        state, record = store.wait(key, request_hash,
                                   started + reliability.DEADLINE_MS / 1000)
    outcome.idempotency = state
    if state == "replay":                         # no dependency call: attempts stays 0
        outcome.status, outcome.raw_body = 200, record["response_body"]
        return
    if state == "conflict":
        outcome.status = 422
        outcome.body = {"error": "idempotency key reused with a different request"}
        return
    if state == "busy":
        outcome.status = 409
        outcome.body = {"error": "a request with this key is still in progress"}
        return
    try:
        result = place_order(order, outcome, key)
    except BaseException:
        store.abandon(key)                        # failed: release the key for a retry
        raise
    raw = json.dumps(result)
    store.complete(key, raw)
    outcome.status, outcome.raw_body = 200, raw


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
            create_order(decode_body(event), outcome, event.get("headers"))
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
    except BreakerOpen:
        outcome.status = 503
        outcome.body = {"error": "dependency unavailable: circuit breaker open"}
    except Exception as err:   # a bug: count it as an error, keep details in the log
        outcome.status, outcome.body = 500, {"error": "internal error"}
        outcome.error_detail = repr(err)

    latency_ms = (time.monotonic() - started) * 1000
    print(json.dumps({
        "type": "request",
        "request_id": context.aws_request_id,
        "run_id": RUN_ID,
        "config": config_name(),
        "env_id": ENV_ID,
        "cold_start": cold,
        "route": route,
        "status": outcome.status,
        "attempts": outcome.attempts,
        "cache": outcome.cache,
        "breaker": _breaker.state if MECH_BREAKER else "off",
        "degraded": outcome.degraded,
        "idempotency": outcome.idempotency,
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
    if MECH_BREAKER:
        headers["X-Breaker"] = _breaker.state
    if outcome.degraded:
        headers["X-Degraded"] = "true"
    if outcome.idempotency == "replay":
        headers["Idempotent-Replay"] = "true"
    body = outcome.raw_body if outcome.raw_body is not None else json.dumps(outcome.body)
    return {"statusCode": outcome.status, "headers": headers, "body": body}
