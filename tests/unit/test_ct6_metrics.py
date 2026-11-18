"""CT6 (midterm Table 6): metrics of a hand-built run equal values computed by hand.

The fixture is 20 measured requests, one every 5 s, with known latencies and
statuses, billed durations, and one outage that ends at 30 s. Every expected
value below was worked out on paper first; the working is in the comments.
"""
import gzip
import json

import pytest

from analysis.metrics import (analyse_experiment, analyse_run, failure_kind, fault_end_ms,
                              is_failure, parse_report, percentile, recovery_time_s,
                              run_metrics, slo_windows, stale_reads)

T0 = 1_700_000_000_000          # first measured request (epoch ms)
CATALOG = "/aws/lambda/b10-catalog"
DEPENDENCY = "/aws/lambda/b10-dependency"

# i: (send s, op, status, latency ms). Outage until 30 s: requests 2-5 fail.
REQUESTS = [
    (0, "get", 200, 40),     # 0  cold start of both functions
    (5, "get", 429, 5),      # 1  throttled by API Gateway: no Lambda ran, no headers
    (10, "get", 503, 15),    # 2  dependency outage
    (15, "get", 503, 12),    # 3  dependency outage
    (20, "post", 503, 14),   # 4  dependency outage (an order)
    (25, "get", 0, 10000),   # 5  no answer within 10 s: no headers
    (30, "get", 200, 800),   # 6  slow right after the outage
    (35, "get", 404, 25),    # 7  unknown product: a real answer, not a failure
    (40, "post", 200, 60),   # 8  order
    (45, "get", 200, 600),   # 9  slow again: breaks the first 30 s of stability
    (50, "get", 200, 35),    # 10 from here on every bucket meets the SLO
    (55, "get", 200, 45),
    (60, "get", 200, 20),
    (65, "get", 200, 50),
    (70, "get", 200, 55),
    (75, "post", 200, 70),   # 15 order
    (80, "get", 200, 30),
    (85, "get", 200, 25),
    (90, "get", 200, 65),
    (95, "get", 200, 28),    # 19
]
NO_HEADERS = {1, 5}
OUTAGE = {2, 3, 4, 5}        # injected 503s (logged by the dependency)
FAULT_END_MS = T0 + 30_000


def report(request_id, billed_ms, init_ms=None):
    line = (f"REPORT RequestId: {request_id}\tDuration: {billed_ms - 0.5} ms\t"
            f"Billed Duration: {billed_ms} ms\tMemory Size: 256 MB\tMax Memory Used: 90 MB")
    return line + (f"\tInit Duration: {init_ms} ms" if init_ms else "")


def fixture():
    records, logs = [], []
    # One warm-up request and its Lambda lines: all must be left out.
    records.append({"t": T0 - 5000, "ph": "warmup", "op": "get", "id": 1, "st": 503, "lat": 9000,
                    "att": "1", "cache": "OFF", "cold": "true", "ver": None, "rid": "w"})
    logs += [{"group": CATALOG, "t": T0 - 4990, "message": report("w", 20)},
             {"group": DEPENDENCY, "t": T0 - 4995, "message": report("dw", 10)},
             {"group": DEPENDENCY, "t": T0 - 4996, "message": json.dumps(
                 {"type": "fault", "failed": True, "delay_ms": 0})}]
    for i, (sent_s, op, status, latency) in enumerate(REQUESTS):
        t = T0 + sent_s * 1000
        headers = i not in NO_HEADERS
        records.append({"t": t, "ph": "main", "op": op, "id": 7, "st": status, "lat": latency,
                        "att": "1" if headers else None, "cache": "OFF" if headers else None,
                        "cold": ("true" if i == 0 else "false") if headers else None,
                        "ver": "1" if status == 200 else None, "rid": f"c{i}" if headers else None})
        if i == 1:
            continue                                  # throttled: neither function ran
        if headers:
            logs.append({"group": CATALOG, "t": t + latency - 1, "message": json.dumps(
                {"type": "request", "request_id": f"c{i}", "status": status, "attempts": 1})})
        catalog_billed = 400 if i == 0 else 5000 if i == 5 else 20
        catalog_end = t + (5000 if i == 5 else latency)       # request 5: Lambda timed out at 5 s
        logs.append({"group": CATALOG, "t": catalog_end,
                     "message": report(f"c{i}", catalog_billed, 350 if i == 0 else None)})
        logs.append({"group": DEPENDENCY, "t": t + 5, "message": report(
            f"d{i}", 300 if i == 0 else 10, 250 if i == 0 else None)})
        if i in OUTAGE:
            logs.append({"group": DEPENDENCY, "t": t + 3, "message": json.dumps(
                {"type": "fault", "failed": True, "delay_ms": 0})})
    # A delayed (not failed) dependency call is not an injected failure.
    logs.append({"group": DEPENDENCY, "t": T0 + 45_003, "message": json.dumps(
        {"type": "fault", "failed": False, "delay_ms": 500})})
    return records, logs


@pytest.fixture
def ct6():
    records, logs = fixture()
    return run_metrics(records, logs, [], fault_end_ms=FAULT_END_MS, origin_ms=T0)


def test_ct6_percentiles_by_nearest_rank(ct6):
    row, _, _ = ct6
    # Sorted latencies: 5 12 14 15 20 | 25 25 28 30 35 | 40 45 50 55 60 | 65 70 600 800 10000
    # rank = ceil(p * 20 / 100): p50 -> 10th = 35, p95 -> 19th = 800, p99 -> 20th = 10000
    assert (row["p50_ms"], row["p95_ms"], row["p99_ms"]) == (35, 800, 10000)


def test_ct6_error_rate_and_failure_kinds(ct6):
    row, _, _ = ct6
    # Failures: 1 (429), 2, 3, 4 (503), 5 (no answer) = 5 of 20; the 404 is a real answer.
    assert (row["requests"], row["failed"], row["successes"]) == (20, 5, 15)
    assert row["error_rate"] == 0.25
    assert (row["fail_timeout"], row["fail_throttled"], row["fail_dependency"],
            row["fail_catalog"], row["fail_platform"]) == (1, 1, 3, 0, 0)


def test_ct6_recovery_time(ct6):
    row, _, _ = ct6
    # Buckets of 5 s from t_end = 30 s: [30,35) has 800 ms (fails), [35,40) ok, [40,45) ok,
    # [45,50) has 600 ms (fails), then [50,55) ... [75,80) all ok: 6 buckets = 30 s.
    # t_rec = 50 s, so R = 50 - 30 = 20 s.
    assert row["recovery_s"] == 20


def test_ct6_cost_per_1000_successful_requests(ct6):
    row, _, _ = ct6
    # Gateway: 20 requests. Invocations: 19 Catalog + 19 dependency (none for the 429) = 38.
    # Billed ms: Catalog 400 + 5000 + 17 * 20 = 5740; dependency 300 + 18 * 10 = 480.
    # GB-s = 6220 / 1000 * 256 / 1024 = 1.555.
    # Orders that reached DynamoDB: requests 8 and 15 -> 2 * 4 = 8 write units.
    # Reads that reached DynamoDB: 19 calls - 2 orders - 4 injected 503s = 13 -> 6.5 read units.
    assert (row["gateway_requests"], row["catalog_invocations"],
            row["dependency_invocations"], row["injected_faults"]) == (20, 19, 19, 4)
    assert row["gb_seconds"] == pytest.approx(1.555, rel=1e-12)
    assert (row["read_units"], row["write_units"]) == (6.5, 8)
    # Cost = 20 * 1.00e-6 + 38 * 0.20e-6 + 1.555 * 0.0000166667 + 6.5 * 0.125e-6 + 8 * 0.625e-6
    #      = 2.0e-5 + 0.76e-5 + 2.59167185e-5 + 0.08125e-5 + 0.5e-5 = 5.93292185e-5 USD
    # C = 1000 * 5.93292185e-5 / 15 successes = 3.95528123333e-3 USD
    assert row["cost_usd"] == pytest.approx(5.93292185e-5, rel=1e-12)
    assert row["cost_per_1000_usd"] == pytest.approx(5.93292185e-2 / 15, rel=1e-12)


def test_ct6_slo_windows_throughput_attempts_cold_starts(ct6):
    row, windows, buckets = ct6
    # Window 0 (0-55 s): 12 requests, 5 failed, p95 = 12th of 12 = 10000 -> fails.
    # Window 1 (60-95 s): 8 requests, none failed, p95 = 8th = 70 ms -> complies.
    assert [(w["requests"], w["failed"], w["p95_ms"], w["meets_slo"]) for w in windows] == \
        [(12, 5, 10000, False), (8, 0, 70, True)]
    assert (row["slo_windows"], row["slo_compliant_windows"], row["slo_compliant_share"]) == \
        (2, 1, 0.5)
    # Phase length: 95 s span * 20 / 19 = 100 s; 15 successes / 100 s = 0.15 per second.
    assert row["duration_s"] == pytest.approx(100.0, rel=1e-12)
    assert row["throughput_rps"] == pytest.approx(0.15, rel=1e-12)
    # 18 requests carry X-Attempts = 1; two had no headers.
    assert (row["mean_attempts"], row["attempts_unknown"]) == (1.0, 2)
    assert (row["cold_starts"], row["dependency_cold_starts"]) == (1, 1)
    assert row["hit_ratio"] is None and row["stale_rate"] is None    # Base: no cache
    assert row["joined_share"] == 1.0
    assert len(buckets) == 20 and all(b["requests"] == 1 for b in buckets)


# ------------------------------------------------------------- further checks

def test_percentile_nearest_rank_small_cases():
    values = list(range(1, 11))
    assert [percentile(values, p) for p in (10, 50, 90, 95, 99, 100)] == [1, 5, 9, 10, 10, 10]
    assert percentile([7], 50) == 7 and percentile([], 95) is None
    assert percentile([3, 1, 2], 50) == 2                      # sorts first


def test_failure_rule_status_by_status():
    statuses = [200, 404, 409, 428, 429, 430, 499, 500, 502, 503, 504, 0]
    assert [is_failure({"st": s}) for s in statuses] == \
        [False, False, False, False, True, False, False, True, True, True, True, True]


def test_an_empty_window_does_not_comply():
    records = [{"t": 0, "st": 200, "lat": 20}, {"t": 130_000, "st": 200, "lat": 20}]
    windows = slo_windows(records, 0)
    assert [(w["requests"], w["meets_slo"]) for w in windows] == [(1, True), (0, False), (1, True)]


def test_recovery_none_when_never_stable_and_zero_when_immediate():
    healthy = [{"t": 1000 * s, "st": 200, "lat": 20} for s in range(0, 40, 5)]
    assert recovery_time_s(healthy, 0) == 0
    assert recovery_time_s(healthy[:5], 0) is None              # only 25 s of data after t_end
    gap = [r for r in healthy if r["t"] != 10_000]              # an empty bucket is no evidence
    assert recovery_time_s(gap, 0) is None
    one_bad = healthy + [{"t": 2000, "st": 503, "lat": 5}]      # 1 of 2 failed in bucket 0
    assert recovery_time_s(one_bad + [{"t": 1000 * s, "st": 200, "lat": 20}
                                      for s in range(40, 45, 5)], 0) == 5


def test_hit_ratio_attempts_and_stale_reads():
    versions = [{"t": 100, "product_id": 7, "version": 5},
                {"t": 200, "product_id": 7, "version": 6}]
    reads = [
        {"t": 50, "id": 7, "cache": "HIT", "ver": "4", "att": "0"},     # no change yet: fresh
        {"t": 150, "id": 7, "cache": "HIT", "ver": "5", "att": "0"},    # newest is 5: fresh
        {"t": 250, "id": 7, "cache": "HIT", "ver": "5", "att": "0"},    # newest is 6: stale
        {"t": 260, "id": 9, "cache": "HIT", "ver": "2", "att": "0"},    # never changed: fresh
        {"t": 270, "id": 7, "cache": "MISS", "ver": "5", "att": "3"},   # not a cached read
        {"t": 280, "id": 7, "cache": "MISS", "ver": None, "att": "4"},  # failed after 4 attempts
        {"t": 290, "id": 3, "cache": "OFF", "ver": "1", "att": "1"},    # an order: not cached
    ]
    assert stale_reads(reads, versions) == (1, 4)
    at_change = [{"t": 200, "id": 7, "cache": "HIT", "ver": "5"}]        # change at the same ms
    assert stale_reads(at_change, versions) == (1, 1)                    # counts as already made
    records = [{**r, "ph": "main", "op": "post" if r["cache"] == "OFF" else "get",
                "st": 200 if r["ver"] else 503, "lat": 10, "cold": "false", "rid": f"r{k}"}
               for k, r in enumerate(reads)]
    row, _, _ = run_metrics(records, [], versions)
    assert row["hit_ratio"] == 4 / 6                              # 4 hits of 6 cached reads
    assert row["mean_attempts"] == 8 / 7                          # (0+0+0+0+3+4+1) / 7
    assert (row["stale_reads"], row["stale_rate"]) == (1, 0.25)


def test_failure_kind_uses_the_join():
    joined = {"c1"}
    assert failure_kind({"st": 503, "rid": "c1"}, joined) == "dependency"
    assert failure_kind({"st": 500, "rid": "c1"}, joined) == "catalog"     # our own bug
    assert failure_kind({"st": 503, "rid": None}, joined) == "platform"    # code never answered
    assert failure_kind({"st": 504, "rid": "x"}, joined) == "platform"
    assert failure_kind({"st": 404, "rid": "c1"}, joined) is None
    assert failure_kind({"st": 0, "rid": None}, joined) == "timeout"


def test_parse_report_line_with_and_without_init():
    warm = parse_report("REPORT RequestId: ab-1\tDuration: 12.34 ms\tBilled Duration: 13 ms\t"
                        "Memory Size: 256 MB\tMax Memory Used: 81 MB")
    assert warm == {"request_id": "ab-1", "duration_ms": 12.34, "billed_ms": 13.0,
                    "memory_mb": 256, "init_ms": None}
    cold = parse_report(report("x", 400, 350.25))
    assert (cold["billed_ms"], cold["duration_ms"], cold["init_ms"]) == (400.0, 399.5, 350.25)


def test_fault_end_only_for_outage():
    assert fault_end_ms({"FAULT_MODE": "outage", "RUN_START": 1000, "FAULT_OUTAGE_END_S": 120,
                         "FAULT_OUTAGE_START_S": 60}) == 1_120_000
    assert fault_end_ms({"FAULT_MODE": "transient", "FAULT_RATE": 0.1}) is None
    assert fault_end_ms({}) is None


def write_gz(path, rows):
    with gzip.open(path, "wt") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_analyse_run_and_experiment_write_csv_files(tmp_path):
    records, logs = fixture()
    run = tmp_path / "exp-001-C7-Base-r1-a1"
    run.mkdir()
    write_gz(run / "records.jsonl.gz", records)
    write_gz(run / "lambda_logs.jsonl.gz", logs)
    write_gz(run / "versions.jsonl.gz", [])
    faults = {"FAULT_MODE": "outage", "RUN_START": T0 // 1000,
              "FAULT_OUTAGE_START_S": 10, "FAULT_OUTAGE_END_S": 30}      # ends at T0 + 30 s
    (run / "manifest.json").write_text(json.dumps({
        "run_id": run.name, "experiment": "exp", "configuration": "Base", "condition": "C7",
        "repetition": 1, "attempt": 1, "valid": True, "faults": faults,
        "run_start": faults["RUN_START"]}))
    row = analyse_run(run)
    assert (row["run_id"], row["condition"], row["recovery_s"], row["p95_ms"]) == \
        (run.name, "C7", 20, 800)
    header = (run / "metrics.csv").read_text().splitlines()[0]
    assert header.startswith("run_id,experiment,configuration,condition")
    assert len((run / "windows.csv").read_text().splitlines()) == 3      # header + 2 windows

    experiment = tmp_path / "exp"
    experiment.mkdir()
    (experiment / "experiment.json").write_text(json.dumps({"valid_runs": [run.name]}))
    assert [r["run_id"] for r in analyse_experiment(experiment)] == [run.name]
    assert len((experiment / "metrics.csv").read_text().splitlines()) == 2


def test_analyse_run_refuses_missing_files(tmp_path):
    (tmp_path / "manifest.json").write_text("{}")
    with pytest.raises(FileNotFoundError, match="records.jsonl.gz"):
        analyse_run(tmp_path)


def test_error_rates_of_reads_and_orders_apart(ct6):
    row, _, _ = ct6
    # Reads: requests 0-3, 5-7, 9-14, 16-19 = 17; failed: 1 (429), 2, 3 (503), 5 (no answer).
    assert (row["read_requests"], row["read_failed"]) == (17, 4)
    assert row["read_error_rate"] == 4 / 17
    # Orders: requests 4, 8, 15 = 3; failed: 4 (503). The warm-up request is not counted.
    assert (row["order_requests"], row["order_failed"]) == (3, 1)
    assert row["order_error_rate"] == 1 / 3


def test_no_orders_gives_no_order_error_rate():
    records = [{"t": 1000 * k, "ph": "main", "op": "get", "st": 200, "lat": 20, "rid": None}
               for k in range(3)]
    row, _, _ = run_metrics(records, [], [])
    assert (row["order_requests"], row["order_failed"], row["order_error_rate"]) == (0, 0, None)
    assert row["read_error_rate"] == 0.0


def test_order_failures_use_the_full_failure_rule():
    # Orders answered 200, 429, no answer (0), 503 and 409: three failures of five;
    # a 409 (out of stock) is a real answer.
    statuses = [200, 429, 0, 503, 409]
    records = [{"t": 1000 * k, "ph": "main", "op": "post", "st": s, "lat": 20, "rid": None}
               for k, s in enumerate(statuses)]
    row, _, _ = run_metrics(records, [], [])
    assert (row["order_requests"], row["order_failed"], row["order_error_rate"]) == (5, 3, 0.6)
    assert row["read_requests"] == 0 and row["read_error_rate"] is None


@pytest.mark.parametrize("offset_ms", [-1500, 0, 1500])
def test_slo_windows_follow_the_fault_schedule_not_the_first_request(offset_ms):
    # One request per second for 180 s, starting offset_ms before or after the planned
    # start; every request sent 60-120 s after the planned start fails (the C7 outage).
    origin = 1_700_000_000_000
    records = []
    for k in range(180):
        t = origin + offset_ms + 1000 * k
        down = 60_000 <= t - origin < 120_000
        records.append({"t": t, "ph": "main", "op": "get", "st": 503 if down else 200,
                        "lat": 20, "rid": None})
    row, windows, _ = run_metrics(records, [], [], origin_ms=origin)
    # Counted from the planned start, the outage fills exactly minute 2, whatever the
    # offset: minutes 1 and 3 pass, minute 2 fails. Counted from the first request, a
    # 1.5 s late start would have pushed outage failures into minute 1 as well.
    assert [w["meets_slo"] for w in windows] == [True, False, True]
    assert (row["slo_windows"], row["slo_compliant_windows"]) == (3, 2)
    assert sum(w["requests"] for w in windows) == 180          # none lost at the edges
