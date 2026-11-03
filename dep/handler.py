"""Simulated dependency service (module M4): item lookups and stock reservations.

Called by the Catalog API through a Lambda function URL (midterm Figure 1,
arrow 3). Day 03 builds the normal behaviour; the fault injector is added on
Day 04.
"""
import base64
import json
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

PRODUCTS_TABLE = os.environ["PRODUCTS_TABLE"]
ORDERS_TABLE = os.environ["ORDERS_TABLE"]

# Created once per execution environment and reused while it stays warm.
_dynamodb = boto3.client("dynamodb")

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


def reserve(body):
    """POST /reserve: decrement stock and record the order, both or neither."""
    try:
        request = json.loads(body or "{}")
        product_id = int(request["product_id"])
        quantity = int(request["quantity"])
    except (ValueError, KeyError, TypeError):
        return response(400, {"error": "body needs integer product_id and quantity"})
    if quantity < 1:
        return response(400, {"error": "quantity must be at least 1"})

    order_id = str(uuid.uuid4())
    try:
        _dynamodb.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": PRODUCTS_TABLE,
                "Key": {"product_id": {"N": str(product_id)}},
                "UpdateExpression": "SET stock = stock - :q",
                "ConditionExpression": "attribute_exists(product_id) AND stock >= :q",
                "ExpressionAttributeValues": {":q": {"N": str(quantity)}},
            }},
            {"Put": {
                "TableName": ORDERS_TABLE,
                "Item": {
                    "order_id": {"S": order_id},
                    "product_id": {"N": str(product_id)},
                    "quantity": {"N": str(quantity)},
                    "created_at": {"S": datetime.now(timezone.utc).isoformat()},
                },
            }},
        ])
    except ClientError as err:
        if err.response["Error"]["Code"] == "TransactionCanceledException":
            return response(409, {"error": "not enough stock or unknown product"})
        raise
    return response(200, {"order_id": order_id, "product_id": product_id,
                          "quantity": quantity})


def handler(event, context):
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
