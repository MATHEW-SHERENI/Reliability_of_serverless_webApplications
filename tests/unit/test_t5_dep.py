"""T5, midterm Table 1: duplicate transactions create one order and decrement once."""
import hashlib
import importlib.util
import json
import uuid
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError

KEY = "order-key"
ORDER_ID = "o-" + hashlib.sha256(KEY.encode()).hexdigest()[:32]
BODY = {"product_id": 17, "quantity": 1}


@pytest.fixture
def dependency(monkeypatch):
    """Load the real handler with a recording DynamoDB client and no AWS calls."""
    dep = Path(__file__).resolve().parents[2] / "dep"
    monkeypatch.syspath_prepend(str(dep))
    monkeypatch.setenv("PRODUCTS_TABLE", "products")
    monkeypatch.setenv("ORDERS_TABLE", "orders")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    db = Mock()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: db)
    spec = importlib.util.spec_from_file_location("t5_dependency", dep / "handler.py")
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    return handler, db


def _cancel(first, second):
    return ClientError({"Error": {"Code": "TransactionCanceledException"},
                        "CancellationReasons": [{"Code": first}, {"Code": second}]},
                       "TransactWriteItems")


def test_key_conditions_put_on_deterministic_order_id(dependency):
    """A keyed reservation guards the order write in the stock transaction."""
    handler, db = dependency
    response = handler.reserve(json.dumps({**BODY, "idempotency_key": KEY}))
    assert response["statusCode"] == 200 and json.loads(response["body"])["order_id"] == ORDER_ID
    db.transact_write_items.assert_called_once()
    update, write = db.transact_write_items.call_args.kwargs["TransactItems"]
    assert update["Update"]["TableName"] == "products"
    put = write["Put"]
    assert put["TableName"] == "orders" and put["Item"]["order_id"] == {"S": ORDER_ID}
    assert put["ConditionExpression"] == "attribute_not_exists(order_id)"


@pytest.mark.parametrize("first", ["None", "ConditionalCheckFailed"])
def test_duplicate_reads_stored_order_without_second_transaction(dependency, first):
    """A failed order condition wins even if current stock is also insufficient."""
    handler, db = dependency
    db.transact_write_items.side_effect = _cancel(first, "ConditionalCheckFailed")
    db.get_item.return_value = {"Item": {"order_id": {"S": ORDER_ID},
                                         "product_id": {"N": "23"}, "quantity": {"N": "2"}}}
    response = handler.reserve(json.dumps({**BODY, "idempotency_key": KEY}))
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"order_id": ORDER_ID, "product_id": 23, "quantity": 2}
    db.transact_write_items.assert_called_once()
    db.get_item.assert_called_once_with(TableName="orders", Key={"order_id": {"S": ORDER_ID}},
                                        ConsistentRead=True)


def test_only_stock_condition_failed_returns_conflict(dependency):
    """An inventory failure has no prior response to read."""
    handler, db = dependency
    db.transact_write_items.side_effect = _cancel("ConditionalCheckFailed", "None")
    response = handler.reserve(json.dumps({**BODY, "idempotency_key": KEY}))
    assert response["statusCode"] == 409
    db.transact_write_items.assert_called_once()
    db.get_item.assert_not_called()


def test_no_key_uses_fresh_uuid_and_unconditional_put(dependency):
    """Unkeyed writes preserve the legacy order format and transaction."""
    handler, db = dependency
    ids = []
    for _ in range(2):
        response = handler.reserve(json.dumps(BODY))
        assert response["statusCode"] == 200
        oid = json.loads(response["body"])["order_id"]
        assert uuid.UUID(oid).version == 4
        put = db.transact_write_items.call_args.kwargs["TransactItems"][1]["Put"]
        assert put["Item"]["order_id"] == {"S": oid} and "ConditionExpression" not in put
        ids.append(oid)
    assert ids[0] != ids[1]
