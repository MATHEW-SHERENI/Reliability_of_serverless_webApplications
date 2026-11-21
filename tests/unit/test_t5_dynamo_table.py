"""T5 (midterm Table 1): the DynamoDB side of the idempotency store.

The agent-written tests drive the store through MemoryTable; these check that
DynamoTable sends DynamoDB the same rules: claim only if the key is unused or
expired, read strongly consistently, and release only an unfinished claim.
"""
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from idempotency import DynamoTable

ITEM = {"idem_key": "k", "status": "IN_PROGRESS", "request_hash": "h",
        "response_body": None, "response_hash": None, "expires_at": 87_400.7}


def condition_failed(operation):
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, operation)


def test_claim_is_conditional_on_unused_or_expired_key():
    db = Mock()
    assert DynamoTable("idem", db).put_new(ITEM, now=1_000.9) is True
    kwargs = db.put_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == \
        "attribute_not_exists(idem_key) OR expires_at <= :now"
    assert kwargs["ExpressionAttributeValues"] == {":now": {"N": "1000"}}
    # whole seconds for DynamoDB's TTL: 87,400.7 -> 87400; no empty response fields
    assert kwargs["Item"] == {"idem_key": {"S": "k"}, "status": {"S": "IN_PROGRESS"},
                              "request_hash": {"S": "h"}, "expires_at": {"N": "87400"}}


def test_taken_key_is_reported_not_raised():
    db = Mock()
    db.put_item.side_effect = condition_failed("PutItem")
    assert DynamoTable("idem", db).put_new(ITEM, now=1_000) is False


def test_other_errors_are_raised():
    db = Mock()
    db.put_item.side_effect = ClientError({"Error": {"Code": "ThrottlingException"}}, "PutItem")
    with pytest.raises(ClientError):          # throttling must not look like a taken key
        DynamoTable("idem", db).put_new(ITEM, now=1_000)


def test_get_is_consistent_and_converts_types():
    db = Mock()
    db.get_item.return_value = {"Item": {
        "idem_key": {"S": "k"}, "status": {"S": "COMPLETED"}, "request_hash": {"S": "h"},
        "response_body": {"S": "{}"}, "response_hash": {"S": "x"}, "expires_at": {"N": "87400"}}}
    assert DynamoTable("idem", db).get("k") == {
        "idem_key": "k", "status": "COMPLETED", "request_hash": "h",
        "response_body": "{}", "response_hash": "x", "expires_at": 87400}
    assert db.get_item.call_args.kwargs == {"TableName": "idem", "Key": {"idem_key": {"S": "k"}},
                                            "ConsistentRead": True}


def test_get_of_a_claim_without_response_and_of_no_record():
    db = Mock()
    db.get_item.return_value = {"Item": {
        "idem_key": {"S": "k"}, "status": {"S": "IN_PROGRESS"}, "request_hash": {"S": "h"},
        "expires_at": {"N": "87400"}}}
    record = DynamoTable("idem", db).get("k")
    assert record["response_body"] is None and record["response_hash"] is None
    db.get_item.return_value = {}
    assert DynamoTable("idem", db).get("k") is None


def test_complete_sets_status_body_and_hash():
    db = Mock()
    DynamoTable("idem", db).complete("k", '{"a":1}', "hash")
    kwargs = db.update_item.call_args.kwargs
    assert kwargs["ExpressionAttributeNames"] == {"#s": "status"}      # a reserved word
    assert kwargs["ExpressionAttributeValues"] == {
        ":done": {"S": "COMPLETED"}, ":body": {"S": '{"a":1}'}, ":hash": {"S": "hash"}}


def test_release_only_deletes_an_unfinished_claim():
    db = Mock()
    DynamoTable("idem", db).delete_in_progress("k")
    kwargs = db.delete_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == "#s = :pending"
    assert kwargs["ExpressionAttributeValues"] == {":pending": {"S": "IN_PROGRESS"}}
    # a claim that has completed meanwhile fails the condition: kept, no error
    db.delete_item.side_effect = condition_failed("DeleteItem")
    DynamoTable("idem", db).delete_in_progress("k")
