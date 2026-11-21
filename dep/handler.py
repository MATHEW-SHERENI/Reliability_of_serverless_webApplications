"""Simulated dependency service (module M4): item lookups and stock reservations.

Called by the Catalog API through a Lambda function URL (midterm Figure 1,
arrow 3). Day 03 builds the normal behaviour; the fault injector is added on
Day 04.
"""
import base64
import hashlib
import json
import os
import re
import random
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from faults import FaultConfig, decide_fault

PRODUCTS_TABLE = os.environ["PRODUCTS_TABLE"]
ORDERS_TABLE = os.environ["ORDERS_TABLE"]

# Created once per execution environment and reused while it stays warm.
_dynamodb = boto3.client("dynamodb")
# Fault settings are read once per execution environment (at cold start).
FAULTS = FaultConfig.from_env(os.environ)
RUN_ID = os.environ.get("RUN_ID", "none")
ENV_ID = str(uuid.uuid4())
# Each environment gets its own random stream, seeded from the run's seed.
_rng = random.Random(f"{os.environ.get('FAULT_SEED', '5360')}-{ENV_ID}")
ITEM_PATH = re.compile(r"^/items/(\d+)$")


def response(status, body):
    """Build the response format that Lambda function URLs expect."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def to_product(item):
    """Turn a DynamoDB item (typed values like {"N": "42"}) into plain JSON values."""
    return {
        "product_id": int(item["product_id"]["N"]),
        "name": item["name"]["S"],
        "price": item["price"]["N"],
        "stock": int(item["stock"]["N"]),
        "version": int(item["version"]["N"]),
    }


def get_item(product_id):
    """GET /items/{id}: return one product, or 404 if it does not exist."""
    result = _dynamodb.get_item(
        TableName=PRODUCTS_TABLE,
        Key={"product_id": {"N": str(product_id)}},
    )
    if "Item" not in result:
        return response(404, {"error": "item not found"})
    return response(200, to_product(result["Item"]))


def order_id_for(key):
    """Task T5: the order ID a key always gets, so a key can place only one order."""
    return "o-" + hashlib.sha256(key.encode()).hexdigest()[:32]


def stored_order(order_id):
    """The saved order, as POST /reserve answered it the first time."""
    item = _dynamodb.get_item(TableName=ORDERS_TABLE, Key={"order_id": {"S": order_id}},
                              ConsistentRead=True)["Item"]
    return {"order_id": item["order_id"]["S"], "product_id": int(item["product_id"]["N"]),
            "quantity": int(item["quantity"]["N"])}


def reserve(body):
    """POST /reserve: decrement stock and record the order, both or neither.

    With an idempotency key (task T5), the order ID comes from the key and the
    order is written only if that ID is new. A repeat with the same key, even a
    retry after a lost answer, then finds the order already there and gets it
    back without a second stock decrement.
    """
    try:
        request = json.loads(body or "{}")
        product_id = int(request["product_id"])
        quantity = int(request["quantity"])
    except (ValueError, KeyError, TypeError):
        return response(400, {"error": "body needs integer product_id and quantity"})
    if quantity < 1:
        return response(400, {"error": "quantity must be at least 1"})

    key = request.get("idempotency_key")
    order_id = order_id_for(key) if key else str(uuid.uuid4())
    put = {
        "TableName": ORDERS_TABLE,
        "Item": {
            "order_id": {"S": order_id},
            "product_id": {"N": str(product_id)},
            "quantity": {"N": str(quantity)},
            "created_at": {"S": datetime.now(timezone.utc).isoformat()},
        },
    }
    if key:
        put["ConditionExpression"] = "attribute_not_exists(order_id)"
    try:
        _dynamodb.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": PRODUCTS_TABLE,
                "Key": {"product_id": {"N": str(product_id)}},
                "UpdateExpression": "SET stock = stock - :q",
                "ConditionExpression": "attribute_exists(product_id) AND stock >= :q",
                "ExpressionAttributeValues": {":q": {"N": str(quantity)}},
            }},
            {"Put": put},
        ])
    except ClientError as err:
        if err.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = err.response.get("CancellationReasons") or []
            # Reason 2 is the order Put: its condition fails only if this key's
            # order exists. That wins even if stock has also run out since.
            if key and len(reasons) > 1 and reasons[1].get("Code") == "ConditionalCheckFailed":
                return response(200, stored_order(order_id))
            return response(409, {"error": "not enough stock or unknown product"})
        raise
    return response(200, {"order_id": order_id, "product_id": product_id,
                          "quantity": quantity})

def log_fault(fault):
    """One JSON line per injected fault: the ground truth for later analysis."""
    print(json.dumps({
        "type": "fault",
        "run_id": RUN_ID,
        "env_id": ENV_ID,
        "mode": FAULTS.mode,
        "failed": fault.fail,
        "delay_ms": fault.delay_ms,
        "at": time.time(),
    }))
def handler(event, context):
    fault = decide_fault(time.time(), _rng, FAULTS)
    if fault.fail or fault.delay_ms:
        log_fault(fault)
    if fault.delay_ms:
        time.sleep(fault.delay_ms / 1000)
    if fault.fail:
        return response(503, {"error": "injected fault"})
    """Entry point: route the request by method and path."""
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")
    body = event.get("body")
    if body and event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()

    match = ITEM_PATH.match(path)
    if method == "GET" and match:
        return get_item(int(match.group(1)))
    if method == "POST" and path == "/reserve":
        return reserve(body)
    return response(404, {"error": f"no route for {method} {path}"})
