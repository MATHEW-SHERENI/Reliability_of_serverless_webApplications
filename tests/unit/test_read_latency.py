"""H2 asks about read latency: p95 of reads alone, apart from the orders."""
from analysis.metrics import run_metrics


def request(t, op, lat):
    return {"t": t, "ph": "main", "op": op, "id": 1, "st": 200, "lat": lat,
            "cache": "MISS" if op == "get" else None, "ver": "1", "rid": None}


def test_read_p95_leaves_the_orders_out():
    records = [request(1000 * k, "get", k) for k in range(1, 21)]       # reads of 1..20 ms
    records += [request(30_000, "post", 1000), request(31_000, "post", 1000)]
    row, _, _ = run_metrics(records, [], [])
    # reads alone: rank ceil(0.95 * 20) = 19 -> 19 ms
    assert row["read_p95_ms"] == 19
    # all 22 requests: rank ceil(0.95 * 22) = 21 -> the first 1,000 ms order
    assert row["p95_ms"] == 1000


def test_no_reads_no_read_p95():
    row, _, _ = run_metrics([request(1000, "post", 50)], [], [])
    assert row["read_p95_ms"] is None
