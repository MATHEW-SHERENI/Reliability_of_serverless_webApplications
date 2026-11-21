"""T5, midterm Table 1: shared keys yield one order and identical replay bodies."""
import hashlib

import pytest

from test_app import FakeClock


def _store(clock):
    import idempotency as idem

    table = idem.MemoryTable()
    return idem.IdempotencyStore(table, clock, wall_time=clock.now), table


@pytest.mark.parametrize("key, valid", [
    ("!", True), ("~" * 128, True), ("", False), ("a" * 129, False),
    ("a b", False), ("é", False), (None, False), ("\x1f", False), ("\x7f", False),
], ids=["minimum", "maximum", "empty", "long", "space", "unicode", "none", "control", "del"])
def test_key_boundaries(key, valid):
    """Check both ends of the printable range and the length limits."""
    import idempotency as idem

    assert idem.valid_key(key) is valid


def test_canonical_hash_ignores_key_order():
    """Check both insertion orders against the specified canonical bytes."""
    from idempotency import body_hash

    expected = hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
    assert body_hash({"a": 1, "b": 2}) == body_hash({"b": 2, "a": 1}) == expected


def test_new_claim_records_fields_and_wall_clock_expiry():
    """Check the stored claim against the 24-hour wall-clock TTL."""
    import idempotency as idem

    clock = FakeClock()
    table = idem.MemoryTable()
    store = idem.IdempotencyStore(table, clock, wall_time=lambda: 1000)
    assert (idem.TTL_S, idem.POLL_S, idem.MAX_KEY_LENGTH) == (86400, 0.05, 128)
    assert store.begin("key", "hash")[0] == "new"
    assert table.get("key") == dict(idem_key="key", status="IN_PROGRESS",
                                    request_hash="hash", expires_at=1000 + 86400,
                                    response_body=None, response_hash=None)


@pytest.mark.parametrize("completed, same, expected", [
    (False, True, "busy"), (False, False, "conflict"),
    (True, True, "replay"), (True, False, "conflict"),
])
def test_begin_preserves_existing_claim(completed, same, expected):
    """A live claim can be inspected without overwriting its contents."""
    store, table = _store(FakeClock())
    store.begin("key", "original")
    if completed:
        store.complete("key", "{}")
    before = dict(table.get("key"))
    assert store.begin("key", "original" if same else "other")[0] == expected
    assert table.get("key") == before


def test_complete_saves_raw_body_and_canonical_hash():
    """Retain wire formatting while hashing canonical JSON."""
    store, table = _store(FakeClock())
    store.begin("key", "hash")
    raw = '{ "b": 2, "a": 1 }'
    store.complete("key", raw)
    outcome, record = store.begin("key", "hash")
    assert outcome == "replay" and record == table.get("key")
    assert record["response_body"] == raw and record["status"] == "COMPLETED"
    assert record["response_hash"] == hashlib.sha256(b'{"a":1,"b":2}').hexdigest()


@pytest.mark.parametrize("completed", [False, True])
def test_abandon_only_removes_pending_claims(completed):
    """A late abandon must preserve the winning response."""
    store, table = _store(FakeClock())
    store.begin("key", "hash")
    if completed:
        store.complete("key", "{}")
    store.abandon("key")
    if completed:
        assert table.get("key")["status"] == "COMPLETED"
    else:
        assert table.get("key") is None


def test_begin_reclaims_at_exact_expiry():
    """A record is available at the TTL boundary even before physical deletion."""
    clock = FakeClock()
    store, table = _store(clock)
    store.begin("key", "old")
    store.complete("key", "{}")
    clock.advance(86400)
    assert store.begin("key", "fresh")[0] == "new"
    assert table.get("key")["request_hash"] == "fresh"


@pytest.mark.parametrize("action, request_hash, expected", [
    ("complete", "same", "replay"), ("complete", "other", "conflict"),
    ("abandon", "same", "new"),
])
def test_wait_observes_owner_transition(action, request_hash, expected, monkeypatch):
    """Change ownership state during the first poll interval."""
    clock = FakeClock()
    store, table = _store(clock)
    store.begin("key", "same")

    def sleep(seconds):
        assert seconds == 0.05
        clock.advance(seconds)
        if action == "complete":
            store.complete("key", "{}")
        else:
            store.abandon("key")

    monkeypatch.setattr(clock, "sleep", sleep)
    outcome, record = store.wait("key", request_hash, deadline=3)
    assert outcome == expected
    if expected == "replay":
        assert record["response_body"] == "{}"
    if expected == "new":
        assert table.get("key")["status"] == "IN_PROGRESS"


def test_wait_returns_busy_at_deadline():
    """Bound the delay to the deadline plus at most one poll."""
    clock = FakeClock()
    store, _ = _store(clock)
    store.begin("key", "same")
    assert store.wait("key", "same", deadline=3)[0] == "busy"
    assert 3 <= clock.now() <= 3 + 0.05 + 1e-9
