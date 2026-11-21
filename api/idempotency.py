"""Task T5: idempotent POST /orders with client-supplied keys (midterm Table 1).

The first request with a new Idempotency-Key claims it: an IN_PROGRESS record is
written only if no live record has that key (midterm Figure 2(b): "put only if
key not present"). When its order succeeds, the record is COMPLETED with the
exact response body, and every later request with the same key gets that body
back unchanged, without calling the dependency. A request that arrives while
the first is still running waits for it; a key reused for a different order is
refused; a failed first request releases the key, so the client can retry.

Records live 24 hours. DynamoDB deletes expired items only eventually, so an
expired record counts as absent when a new claim is made.

The store logic (IdempotencyStore) is the same for the real DynamoDB table and
for MemoryTable, the in-memory stand-in that the unit tests use.
"""
import hashlib
import json
import time

TTL_S = 86_400            # a key is remembered for 24 hours
POLL_S = 0.05             # how often a waiting request looks at the record again
MAX_KEY_LENGTH = 128
IN_PROGRESS, COMPLETED = "IN_PROGRESS", "COMPLETED"


def valid_key(key):
    """True for 1 to 128 printable ASCII characters without spaces (! to ~)."""
    return (isinstance(key, str) and 1 <= len(key) <= MAX_KEY_LENGTH
            and all("!" <= ch <= "~" for ch in key))


def body_hash(body):
    """SHA-256 of the canonical JSON of body, so key order does not matter."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class MemoryTable:
    """In-memory table with the same four operations as DynamoTable."""

    def __init__(self):
        self._items = {}

    def put_new(self, item, now):
        """Write item if its key is unused or the existing record has expired."""
        existing = self._items.get(item["idem_key"])
        if existing is not None and existing["expires_at"] > now:
            return False
        self._items[item["idem_key"]] = dict(item)
        return True

    def get(self, key):
        item = self._items.get(key)
        return None if item is None else dict(item)

    def complete(self, key, response_body, response_hash):
        item = self._items.get(key)
        if item is not None:
            item.update(status=COMPLETED, response_body=response_body,
                        response_hash=response_hash)

    def delete_in_progress(self, key):
        item = self._items.get(key)
        if item is not None and item["status"] == IN_PROGRESS:
            del self._items[key]


class DynamoTable:
    """The idempotency table in DynamoDB. Every read is strongly consistent,
    because a waiting request must see the first request's completion at once."""

    def __init__(self, name, client=None):
        if client is None:
            import boto3                       # only needed when T5 is switched on
            client = boto3.client("dynamodb")
        self._name, self._db = name, client

    def put_new(self, item, now):
        values = {"idem_key": {"S": item["idem_key"]}, "status": {"S": item["status"]},
                  "request_hash": {"S": item["request_hash"]},
                  # DynamoDB's TTL needs whole epoch seconds
                  "expires_at": {"N": str(int(item["expires_at"]))}}
        try:
            self._db.put_item(
                TableName=self._name, Item=values,
                ConditionExpression="attribute_not_exists(idem_key) OR expires_at <= :now",
                ExpressionAttributeValues={":now": {"N": str(int(now))}})
        except Exception as err:
            if _condition_failed(err):
                return False
            raise
        return True

    def get(self, key):
        found = self._db.get_item(TableName=self._name, Key={"idem_key": {"S": key}},
                                  ConsistentRead=True).get("Item")
        if found is None:
            return None
        return {"idem_key": found["idem_key"]["S"], "status": found["status"]["S"],
                "request_hash": found["request_hash"]["S"],
                "response_body": found.get("response_body", {}).get("S"),
                "response_hash": found.get("response_hash", {}).get("S"),
                "expires_at": int(found["expires_at"]["N"])}

    def complete(self, key, response_body, response_hash):
        self._db.update_item(
            TableName=self._name, Key={"idem_key": {"S": key}},
            UpdateExpression="SET #s = :done, response_body = :body, response_hash = :hash",
            ExpressionAttributeNames={"#s": "status"},      # status is a reserved word
            ExpressionAttributeValues={":done": {"S": COMPLETED}, ":body": {"S": response_body},
                                       ":hash": {"S": response_hash}})

    def delete_in_progress(self, key):
        try:
            self._db.delete_item(
                TableName=self._name, Key={"idem_key": {"S": key}},
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":pending": {"S": IN_PROGRESS}})
        except Exception as err:
            if not _condition_failed(err):     # completed or gone: nothing to release
                raise


def _condition_failed(err):
    response = getattr(err, "response", None) or {}
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class IdempotencyStore:
    """Claims, completes, and releases keys; tells a request what to do.

    begin() and wait() return (outcome, record):
      "new"       this request owns the key and should place the order
      "replay"    the order with this key is done: send record["response_body"]
      "conflict"  the key was used for a different order: refuse
      "busy"      another request with this key is still running
    """

    def __init__(self, table, clock, wall_time=time.time):
        self._table, self._clock, self._wall_time = table, clock, wall_time

    def _claim(self, key, request_hash):
        """Try to claim the key; return the new record, or None if it is taken."""
        now = self._wall_time()
        item = {"idem_key": key, "status": IN_PROGRESS, "request_hash": request_hash,
                "response_body": None, "response_hash": None, "expires_at": now + TTL_S}
        return item if self._table.put_new(item, now) else None

    def begin(self, key, request_hash):
        """Claim the key, or report what the existing record means for this request."""
        existing = None
        for _ in range(3):      # a record released between our put and get: claim again
            item = self._claim(key, request_hash)
            if item is not None:
                return "new", item
            existing = self._table.get(key)
            if existing is not None:
                break
        if existing is None:
            return "busy", None                # keeps changing hands: wait() decides
        if existing["request_hash"] != request_hash:
            return "conflict", existing
        return ("replay" if existing["status"] == COMPLETED else "busy"), existing

    def wait(self, key, request_hash, deadline):
        """Wait (on clock) until the running request finishes or deadline passes.

        If it completes, the result is a replay (or a conflict for a different
        order); if it releases the key, this request claims it and is "new".
        """
        existing = None
        while self._clock.now() < deadline:
            existing = self._table.get(key)
            if existing is None:
                item = self._claim(key, request_hash)
                if item is not None:
                    return "new", item
                continue
            if existing["status"] == COMPLETED:
                outcome = "replay" if existing["request_hash"] == request_hash else "conflict"
                return outcome, existing
            self._clock.sleep(POLL_S)
        return "busy", existing

    def complete(self, key, response_body):
        """Store the exact body sent to the client, and its canonical hash."""
        self._table.complete(key, response_body, body_hash(json.loads(response_body)))

    def abandon(self, key):
        """Release a claim whose order failed, unless it has already completed."""
        self._table.delete_in_progress(key)
