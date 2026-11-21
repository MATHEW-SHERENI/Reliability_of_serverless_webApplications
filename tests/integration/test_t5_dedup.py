"""T5 (midterm Table 1). 1,000 same-key requests from 50 clients produce one stored order, one stock decrement, and 999 replays."""
import hashlib
import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
import pytest
import urllib3
from boto3.dynamodb.conditions import Attr

from orchestrate.fault_config import set_mechanisms

pytestmark = pytest.mark.integration


def tf_output(name):
    """Return a deployed stack output for the completion check."""
    return subprocess.run(["terraform", "-chdir=infra", "output", "-raw", name],
                          capture_output=True, text=True, check=True).stdout


def table(name):
    """Return the deployed DynamoDB table identified by its output name."""
    return boto3.resource("dynamodb", region_name="us-east-2").Table(tf_output(name))


def _orders_since(orders, start):
    options = {"ConsistentRead": True,
               "FilterExpression": Attr("product_id").eq(17) & Attr("created_at").gte(start)}
    while True:
        page = orders.scan(**options)
        yield from page["Items"]
        if not page.get("LastEvaluatedKey"):
            return
        options["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def test_one_thousand_posts_create_one_order_and_decrement_stock_once():
    """Verify wire responses, stock, orders, and the completed claim together."""
    from idempotency import body_hash

    function = tf_output("catalog_function_name")
    products, orders, claims = table("products_table"), table("orders_table"), table("idempotency_table")
    url = tf_output("api_url").rstrip("/") + "/orders"
    key = f"t5-{uuid.uuid4()}"
    headers = {"Content-Type": "application/json", "Idempotency-Key": key}
    body = json.dumps({"product_id": 17, "quantity": 1}).encode()
    http = urllib3.PoolManager(maxsize=50, block=True)

    def post(index):
        return http.request("POST", url, headers=headers, body=body, retries=False, timeout=10)

    def stock():
        return products.get_item(Key={"product_id": 17}, ConsistentRead=True)["Item"]["stock"]

    try:
        set_mechanisms(function, retry=False, cache=False, idempotency=True)
        before = stock()
        # Allow for the laptop clock running ahead of Lambda when it timestamps the order.
        start = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        with ThreadPoolExecutor(max_workers=50) as pool:
            responses = list(pool.map(post, range(1000)))
        assert len(responses) == 1000 and all(r.status == 200 for r in responses)
        assert len({hashlib.sha256(r.data).digest() for r in responses}) == 1
        replay = [r.headers.get("Idempotent-Replay") for r in responses]
        assert replay.count("true") == 999 and replay.count(None) == 1
        assert stock() == before - 1
        result = json.loads(responses[0].data)
        assert [r["order_id"] for r in _orders_since(orders, start)] == [result["order_id"]]
        claim = claims.get_item(Key={"idem_key": key}, ConsistentRead=True)["Item"]
        assert claim["status"] == "COMPLETED" and claim["response_hash"] == body_hash(result)
    finally:
        http.clear()
        set_mechanisms(function, retry=False, cache=False, breaker=False, idempotency=False)
