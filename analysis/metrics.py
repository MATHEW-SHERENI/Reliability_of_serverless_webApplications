"""Per-run metrics (M7): from one run's raw files to the numbers the report uses.

Inputs, in results/<run_id>/ (written by the orchestrator, M6):
  records.jsonl.gz      one line per request, from k6 (the client's view)
  lambda_logs.jsonl.gz  REPORT lines and JSON log lines of both functions
  versions.jsonl.gz     every price change of the updater (ground truth for staleness)
  manifest.json         configuration, condition, repetition and fault schedule

Outputs, next to them:
  metrics.csv   one row with every metric of midterm Section 4.4
  windows.csv   one row per 1-minute SLO window
  buckets.csv   one row per 5-second bucket (error rate and p99 over time, for C7)

Definitions (midterm Section 4.4):
  - Only the measured phase counts; warm-up requests are left out.
  - A request fails if its status is >= 500 or 429, or it got no answer at all
    (status 0: the 10 s client timeout or a broken connection).
  - Percentiles use the nearest rank, over all measured requests, failed ones included.
  - A 1-minute window complies with the SLO if p95 <= 500 ms and error rate <= 1%.
  - Recovery time R = t_rec - t_end. t_end is when the injected outage stops (from
    the fault schedule); buckets of 5 s start at t_end; t_rec is the start of the
    first bucket from which 6 buckets in a row (30 s) all meet both thresholds.
  - Cost per 1,000 successful requests, C, at us-east-2 list prices.

How the Lambda records are used:
  - Catalog log lines are joined to client requests by request ID. The join tells
    a 5xx that the Catalog itself returned (the dependency failed or timed out)
    from one that never reached our code (API Gateway or Lambda failed).
  - Dependency calls have no ID the client sees, so invocations, billed GB-seconds
    and injected faults are counted for every REPORT or fault line whose time lies
    inside the measured phase. The same is done for the Catalog, so both functions
    are counted the same way.
  - DynamoDB request units are estimated: a product read is one GetItem
    (eventually consistent, under 4 KB: 0.5 read units); an order is one
    transaction of two items under 1 KB (2 write units each: 4). A dependency call
    stopped by an injected 503 never reaches DynamoDB. The price updater's writes
    belong to the experiment, not to the API, and are not counted.

Usage:
  .venv/bin/python analysis/metrics.py results/<run_id>          one run
  .venv/bin/python analysis/metrics.py results/<experiment_id>   every valid run of an experiment
"""
import argparse
import bisect
import csv
import gzip
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# us-east-2 list prices in US dollars (midterm Section 4.4).
PRICE_GATEWAY_REQUEST = 1.00 / 1_000_000       # HTTP API
PRICE_INVOCATION = 0.20 / 1_000_000            # Lambda
PRICE_GB_SECOND = 0.0000166667                 # Lambda, x86
PRICE_READ_UNIT = 0.125 / 1_000_000            # DynamoDB on demand
PRICE_WRITE_UNIT = 0.625 / 1_000_000

READ_UNITS_PER_READ = 0.5      # GetItem, eventually consistent, item under 4 KB
WRITE_UNITS_PER_ORDER = 4      # transaction of 2 items under 1 KB, 2 units each
ORDER_REACHED_DB = {200, 409, 504}   # answered by the transaction, or still running when the
                                     # Catalog gave up (504): DynamoDB was used in all three

SLO_P95_MS = 500
SLO_ERROR_RATE = 0.01
WINDOW_S = 60
BUCKET_S = 5
STABLE_S = 30

REPORT_FIELDS = {
    "request_id": (r"RequestId: (\S+)", str),
    "duration_ms": (r"(?:^|\t)Duration: ([\d.]+) ms", float),
    "billed_ms": (r"Billed Duration: ([\d.]+) ms", float),
    "memory_mb": (r"Memory Size: (\d+) MB", int),
    "init_ms": (r"Init Duration: ([\d.]+) ms", float),
}


# ------------------------------------------------------------------ reading files

def read_jsonl(path):
    """Every JSON line of a file (.gz or plain); other lines are skipped."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as f:
        return [json.loads(line) for line in f if line.startswith("{")]


def parse_report(message):
    """The fields of one Lambda REPORT line; init_ms is None for a warm start."""
    fields = {}
    for name, (pattern, kind) in REPORT_FIELDS.items():
        match = re.search(pattern, message)
        fields[name] = kind(match.group(1)) if match else None
    return fields


def role_of(group):
    if group.endswith("-catalog"):
        return "catalog"
    if group.endswith("-dependency"):
        return "dependency"
    return None


def parse_lambda_logs(lines):
    """Split exported log lines into REPORT lines and JSON event lines, by function."""
    reports, events = [], []
    for line in lines:
        role, message = role_of(line["group"]), line["message"]
        if message.startswith("REPORT"):
            reports.append({"role": role, "t": line["t"], **parse_report(message)})
        elif message.startswith("{"):
            try:
                event = json.loads(message)
            except ValueError:
                continue
            events.append({**event, "role": role, "t": line["t"]})
    return reports, events


# -------------------------------------------------------------- basic definitions

def is_failure(record):
    status = record["st"]
    return status == 0 or status == 429 or status >= 500


def failure_kind(record, catalog_ids):
    """Why a failed request failed; None for a success."""
    status = record["st"]
    if status == 0:
        return "timeout"
    if status == 429:
        return "throttled"
    if status < 500:
        return None
    if record.get("rid") not in catalog_ids:
        return "platform"            # our code never answered: API Gateway or Lambda failed
    return "dependency" if status in (502, 503, 504) else "catalog"


def percentile(values, p):
    """Nearest-rank percentile: the smallest value with at least p% of values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (p * len(ordered) + 99) // 100          # ceil(p * n / 100) in whole numbers
    return ordered[max(rank, 1) - 1]


def summarise(records):
    """Requests, failures, error rate and latency percentiles of a group of requests."""
    latencies = [r["lat"] for r in records]
    failed = sum(is_failure(r) for r in records)
    return {"requests": len(records), "failed": failed,
            "error_rate": failed / len(records) if records else None,
            "p50_ms": percentile(latencies, 50), "p95_ms": percentile(latencies, 95),
            "p99_ms": percentile(latencies, 99)}


def meets_slo(summary):
    """An empty group meets nothing: no answers is no evidence of health."""
    return (summary["requests"] > 0 and summary["p95_ms"] <= SLO_P95_MS
            and summary["error_rate"] <= SLO_ERROR_RATE)


def by_interval(records, origin_ms, size_s):
    """Groups of requests by send time: index k holds [origin + k*size, origin + (k+1)*size)."""
    groups = {}
    for r in records:
        groups.setdefault((r["t"] - origin_ms) // (size_s * 1000), []).append(r)
    return groups


def intervals(records, origin_ms, size_s):
    """One summary per interval, from the first to the last, empty ones included."""
    groups = by_interval(records, origin_ms, size_s)
    if not groups:
        return []
    rows = []
    for k in range(min(groups), max(groups) + 1):
        summary = summarise(groups.get(k, []))
        rows.append({"index": k, "start_s": k * size_s, **summary, "meets_slo": meets_slo(summary)})
    return rows


def slo_windows(records, start_ms):
    """1-minute windows of the measured phase, from its first request."""
    return intervals(records, start_ms, WINDOW_S)


def recovery_time_s(records, fault_end_ms):
    """R in seconds, or None if the run never held the SLO for 30 s after the fault ended."""
    groups = by_interval(records, fault_end_ms, BUCKET_S)   # buckets before t_end get k < 0
    if not groups:
        return None
    ok = {k: meets_slo(summarise(group)) for k, group in groups.items()}
    needed = STABLE_S // BUCKET_S
    for j in range(max(groups) + 1):
        if all(ok.get(j + i, False) for i in range(needed)):
            return j * BUCKET_S
    return None


def stale_reads(records, versions):
    """(stale, hits): cache hits whose data version is older than the newest one at send time."""
    history = {}
    for change in sorted(versions, key=lambda v: v["t"]):
        history.setdefault(change["product_id"], []).append((change["t"], change["version"]))
    hits = [r for r in records if r.get("cache") == "HIT"]
    stale = 0
    for r in hits:
        changes = history.get(r["id"], [])
        k = bisect.bisect_right([t for t, _ in changes], r["t"])
        # No change yet: the cached copy was read during this run, so it is current.
        if k and r.get("ver") is not None and int(r["ver"]) < changes[k - 1][1]:
            stale += 1
    return stale, len(hits)


def phase_seconds(records):
    """Length of the measured phase: the span of send times plus one average gap."""
    if len(records) < 2:
        return None
    times = [r["t"] for r in records]
    return (max(times) - min(times)) / 1000 * len(records) / (len(records) - 1)


def cost_usd(gateway_requests, invocations, gb_seconds, read_units, write_units):
    return (gateway_requests * PRICE_GATEWAY_REQUEST + invocations * PRICE_INVOCATION
            + gb_seconds * PRICE_GB_SECOND + read_units * PRICE_READ_UNIT
            + write_units * PRICE_WRITE_UNIT)


# ------------------------------------------------------------------ one run

def run_metrics(records, log_lines, versions, fault_end_ms=None, origin_ms=None):
    """All metrics of one run. Returns (row, windows, buckets).

    records: k6 lines; log_lines: exported Lambda lines; versions: price changes;
    fault_end_ms: when the injected outage stopped (C7 only); origin_ms: the
    planned start of the measured phase, used to align the 5 s buckets.
    """
    measured = [r for r in records if r["ph"] != "warmup"]
    if not measured:
        raise ValueError("no measured-phase requests")
    start_ms = min(r["t"] for r in measured)
    end_ms = max(r["t"] + r["lat"] for r in measured)

    reports, events = parse_lambda_logs(log_lines)
    inside = lambda line: start_ms <= line["t"] <= end_ms           # noqa: E731
    catalog_reports = [x for x in reports if x["role"] == "catalog" and inside(x)]
    dep_reports = [x for x in reports if x["role"] == "dependency" and inside(x)]
    catalog_ids = {e["request_id"] for e in events
                   if e["role"] == "catalog" and e.get("type") == "request"}
    injected = sum(1 for e in events if e["role"] == "dependency" and e.get("type") == "fault"
                   and e.get("failed") and inside(e))

    overall = summarise(measured)
    successes = overall["requests"] - overall["failed"]
    kinds = [failure_kind(r, catalog_ids) for r in measured]
    with_rid = [r for r in measured if r.get("rid")]
    known = [int(r["att"]) for r in measured if r.get("att") is not None]
    cached = [r for r in measured if r.get("cache") in ("HIT", "MISS")]
    hits = sum(r["cache"] == "HIT" for r in cached)
    stale, _ = stale_reads(measured, versions)

    orders = sum(1 for r in measured if r["op"] == "post" and r["st"] in ORDER_REACHED_DB)
    reads = max(0, len(dep_reports) - orders - injected)
    read_units, write_units = reads * READ_UNITS_PER_READ, orders * WRITE_UNITS_PER_ORDER
    gb_seconds = sum(x["billed_ms"] / 1000 * x["memory_mb"] / 1024
                     for x in catalog_reports + dep_reports)
    invocations = len(catalog_reports) + len(dep_reports)
    cost = cost_usd(len(measured), invocations, gb_seconds, read_units, write_units)

    windows = slo_windows(measured, start_ms)
    buckets = intervals(measured, origin_ms if origin_ms is not None else start_ms, BUCKET_S)
    duration = phase_seconds(measured)
    row = {
        "requests": overall["requests"], "successes": successes, "failed": overall["failed"],
        "error_rate": overall["error_rate"],
        "p50_ms": overall["p50_ms"], "p95_ms": overall["p95_ms"], "p99_ms": overall["p99_ms"],
        "duration_s": duration,
        "throughput_rps": successes / duration if duration else None,
        "mean_attempts": sum(known) / len(known) if known else None,
        "attempts_unknown": overall["requests"] - len(known),
        "cache_hits": hits, "hit_ratio": hits / len(cached) if cached else None,
        "stale_reads": stale, "stale_rate": stale / hits if hits else None,
        "cold_starts": sum(r.get("cold") == "true" for r in measured),
        "dependency_cold_starts": sum(x["init_ms"] is not None for x in dep_reports),
        "slo_windows": len(windows),
        "slo_compliant_windows": sum(w["meets_slo"] for w in windows),
        "slo_compliant_share": sum(w["meets_slo"] for w in windows) / len(windows),
        "recovery_s": recovery_time_s(measured, fault_end_ms) if fault_end_ms else None,
        **{f"fail_{k}": kinds.count(k) for k in
           ("timeout", "throttled", "dependency", "catalog", "platform")},
        "joined_share": (sum(r["rid"] in catalog_ids for r in with_rid) / len(with_rid)
                         if with_rid else None),
        "gateway_requests": len(measured),
        "catalog_invocations": len(catalog_reports),
        "dependency_invocations": len(dep_reports),
        "injected_faults": injected,
        "gb_seconds": gb_seconds, "read_units": read_units, "write_units": write_units,
        "cost_usd": cost,
        "cost_per_1000_usd": 1000 * cost / successes if successes else None,
    }
    return row, windows, buckets


def fault_end_ms(faults):
    """When the injected outage stops (epoch ms), or None for a condition without one."""
    if not faults or faults.get("FAULT_MODE") != "outage":
        return None
    return (int(faults["RUN_START"]) + int(faults["FAULT_OUTAGE_END_S"])) * 1000


IDENTITY = ("run_id", "experiment", "configuration", "condition", "repetition", "attempt", "valid")


def analyse_run(run_dir):
    """Read one run's files, write its three CSV files, and return its metrics row."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    needed = ["records.jsonl.gz", "lambda_logs.jsonl.gz", "versions.jsonl.gz"]
    missing = [name for name in needed if not (run_dir / name).exists()]
    if missing:
        raise FileNotFoundError(f"{run_dir}: missing {', '.join(missing)}")
    faults = manifest.get("faults") or {}
    origin = int(manifest["run_start"]) * 1000 if manifest.get("run_start") else None
    row, windows, buckets = run_metrics(read_jsonl(run_dir / "records.jsonl.gz"),
                                        read_jsonl(run_dir / "lambda_logs.jsonl.gz"),
                                        read_jsonl(run_dir / "versions.jsonl.gz"),
                                        fault_end_ms(faults), origin)
    row = {**{key: manifest.get(key) for key in IDENTITY}, **row}
    write_csv(run_dir / "metrics.csv", [row])
    write_csv(run_dir / "windows.csv", windows)
    write_csv(run_dir / "buckets.csv", buckets)
    return row


def analyse_experiment(experiment_dir):
    """Metrics of every valid run of an experiment, one row each, in experiment.json order."""
    experiment_dir = Path(experiment_dir)
    record = json.loads((experiment_dir / "experiment.json").read_text())
    rows = [analyse_run(experiment_dir.parent / run_id) for run_id in record["valid_runs"]]
    write_csv(experiment_dir / "metrics.csv", rows)
    return rows


def write_csv(path, rows):
    if not rows:
        Path(path).write_text("")
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def show(row):
    def fmt(value):
        return f"{value:.4g}" if isinstance(value, float) else str(value)
    for key, value in row.items():
        print(f"  {key:24} {fmt(value)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", help="results/<run_id> or results/<experiment_id>")
    args = parser.parse_args()
    folder = Path(args.folder)
    if (folder / "experiment.json").exists():
        rows = analyse_experiment(folder)
        print(f"{len(rows)} valid runs -> {folder / 'metrics.csv'}")
    elif (folder / "manifest.json").exists():
        show(analyse_run(folder))
        print(f"-> {folder / 'metrics.csv'}, windows.csv, buckets.csv")
    else:
        print(f"{folder} has neither manifest.json nor experiment.json. If the run was made "
              "elsewhere, copy it first:\n  aws s3 sync s3://<results_bucket>/runs/<run_id>/ "
              "results/<run_id>/")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
