"""Summarize a k6 record file (one JSON line per request) to check a run by eye.

Usage: .venv/bin/python load/check_records.py results/smoke.jsonl   (or a .jsonl.gz file)

Prints request counts per phase, operation and status, the read share (90%
planned), how often product 1 was read against the Zipf expectation, whether
every answered request carried the diagnostic headers, and nearest-rank latency
percentiles for the measured phase.
"""
import gzip
import json
import math
import sys
from collections import Counter

HEADERS = ("att", "cache", "cold", "env", "rid")


def nearest_rank(sorted_values, p):
    """The smallest value with at least p% of the values at or below it."""
    rank = math.ceil(p / 100 * len(sorted_values))
    return sorted_values[max(rank, 1) - 1]


def main(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        records = [json.loads(line) for line in f if line.startswith("{")]
    if not records:
        sys.exit(f"no records in {path}")

    print(f"{len(records)} records, run {records[0]['run']}, "
          f"condition {records[0]['cond']}, configuration {records[0]['cfg']}")
    print("phase:", dict(Counter(r["ph"] for r in records)))
    print("operation:", dict(Counter(r["op"] for r in records)))
    print("status:", dict(sorted(Counter(r["st"] for r in records).items())))

    reads = [r for r in records if r["op"] == "get"]
    print(f"read share: {len(reads) / len(records):.3f} (planned 0.900)")
    harmonic = sum(1 / k for k in range(1, 1001))
    if reads:
        top = sum(r["id"] == 1 for r in reads) / len(reads)
        print(f"product 1 share of reads: {top:.3f} (Zipf expects {1 / harmonic:.3f})")

    answered = [r for r in records if r["st"] != 0]
    missing = sum(any(r[h] is None for h in HEADERS) for r in answered)
    print(f"answered requests missing a diagnostic header: {missing} of {len(answered)}")

    main_lat = sorted(r["lat"] for r in records if r["ph"] == "main" and r["st"] == 200)
    if main_lat:
        print("measured phase, successful requests: "
              f"p50 {nearest_rank(main_lat, 50):.1f} ms, "
              f"p95 {nearest_rank(main_lat, 95):.1f} ms, "
              f"p99 {nearest_rank(main_lat, 99):.1f} ms")


if __name__ == "__main__":
    main(sys.argv[1])
