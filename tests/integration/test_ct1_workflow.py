"""CT1 (midterm Table 5): the normal workflow through the deployed stack.

Integration test: needs `make deploy && make seed`. Run with `make test-integration`.
"""
import json
import subprocess

import boto3
import pytest
import urllib3

pytestmark = pytest.mark.integration

PRODUCT_ID = 42
DIAGNOSTIC_HEADERS = ("x-cold-start", "x-env-id", "x-request-id",
                      "x-attempts", "x-cache", "x-data-version")


def tf_output(name):
    return subprocess.run(["terraform", "-chdir=infra", "output", "-raw", name],
                          capture_output=True, text=True, check=True).stdout


def table(name):
    return boto3.resource("dynamodb", region_name="us-east-2").Table(tf_output(name))


def stock_of(products):
    item = products.get_item(Key={"product_id": PRODUCT_ID}, ConsistentRead=True)["Item"]
    return int(item["stock"])


def test_ct1_get_then_order_reduces_stock_by_exactly_two():
    api = tf_output("api_url")
    products, orders = table("products_table"), table("orders_table")
    http = urllib3.PoolManager()

    # Warm-up: CT1 checks the normal path, so a possible cold-start 504 is ignored.
    http.request("GET", f"{api}products/{PRODUCT_ID}", timeout=10)
    before = stock_of(products)

    got = http.request("GET", f"{api}products/{PRODUCT_ID}", timeout=10)
    assert got.status == 200
    assert json.loads(got.data)["product_id"] == PRODUCT_ID
    assert all(header in got.headers for header in DIAGNOSTIC_HEADERS)

    placed = http.request("POST", f"{api}orders", timeout=10,
                          body=json.dumps({"product_id": PRODUCT_ID, "quantity": 2}),
                          headers={"Content-Type": "application/json"})
    assert placed.status == 200
    order_id = json.loads(placed.data)["order_id"]

    assert stock_of(products) == before - 2
    saved = orders.get_item(Key={"order_id": order_id}, ConsistentRead=True)["Item"]
    assert saved["product_id"] == PRODUCT_ID and saved["quantity"] == 2
