"""CT5 (midterm Table 5): calibrate the fault injector against the deployed dependency.

Not a pytest test: it takes 3-4 minutes and prints a calibration report.
Needs `make deploy && make seed`. Run with `make ct5`. Exits with 1 if any check fails.

The delay check uses the dependency's own latency (Lambda's REPORT Duration), as the
midterm criterion defines it; the laptop round trip is printed only for information.
"""
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "api")]

from dep_client import DependencyClient, DependencyError  # noqa: E402
from orchestrate.fault_config import set_faults  # noqa: E402

REGION = "us-east-2"
TIMEOUT_S = 5.0
WORKERS = 10    # matches the client's connection pool size
GUARD_S = 1.0   # ignore calls this close to an outage edge


def tf_output(name):
    return subprocess.run(["terraform", "-chdir=infra", "output", "-raw", name],
                          cwd=ROOT, capture_output=True, text=True, check=True).stdout


def report(name, passed, detail):
    print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}", flush=True)
    return passed


def timed_call(client, product_id):
    """One call. Returns (outcome, sent_at wall-clock seconds, latency_ms)."""
    sent_at = time.time()
    started = time.perf_counter()
    try:
        client.get_item(product_id, TIMEOUT_S)
        outcome = "ok"
    except DependencyError as err:
        outcome = "injected" if getattr(err, "status", None) == 503 else "other"
    return outcome, sent_at, (time.perf_counter() - started) * 1000


def run_many(client, count):
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda i: timed_call(client, i % 1000 + 1), range(count)))


def check_transient(client, function):
    print("transient: 10,000 calls at FAULT_RATE=0.10 (about 2 minutes)...", flush=True)
    set_faults(function, FAULT_MODE="transient", FAULT_RATE=0.10, RUN_ID="ct5-transient")
    results = run_many(client, 10_000)
    injected = sum(r[0] == "injected" for r in results)
    other = sum(r[0] == "other" for r in results)
    answered = len(results) - other
    rate = injected / answered
    return report("transient 10%", 0.09 <= rate <= 0.11 and other <= 10,
                  f"{injected} of {answered} failed = {rate:.2%} (other errors: {other})")


def lambda_median_ms(function, start, end, expected):
    """Median Duration Lambda itself reported for invocations ending in [start, end]."""
    logs = boto3.client("logs", region_name=REGION)
    for _ in range(12):                     # log lines arrive a few seconds late
        query_id = logs.start_query(
            logGroupName=f"/aws/lambda/{function}",
            startTime=int(start), endTime=int(end) + 2,
            queryString='filter @type = "REPORT" '
                        '| stats count(*) as calls, pct(@duration, 50) as p50')["queryId"]
        result = logs.get_query_results(queryId=query_id)
        while result["status"] in ("Scheduled", "Running"):
            time.sleep(1)
            result = logs.get_query_results(queryId=query_id)
        row = {f["field"]: f["value"] for f in (result["results"] or [[]])[0]}
        if int(row.get("calls", 0)) >= expected:
            return float(row["p50"])
        time.sleep(5)
    raise RuntimeError("Lambda REPORT lines did not arrive in time")


def measure(client, function, count=300, warmup=40):
    """Return (dependency median from Lambda, laptop round-trip median), in ms."""
    run_many(client, warmup)
    start = time.time()
    results = run_many(client, count)
    end = time.time()
    laptop = statistics.median(r[2] for r in results if r[0] == "ok")
    return lambda_median_ms(function, start, end, count), laptop


def check_delay(client, function):
    print("delay: dependency median (Lambda Duration), healthy vs FAULT_DELAY_MS=300...",
          flush=True)
    set_faults(function, RUN_ID="ct5-healthy")
    healthy, healthy_laptop = measure(client, function)
    set_faults(function, FAULT_MODE="delay", FAULT_DELAY_MS=300, RUN_ID="ct5-delay")
    delayed, delayed_laptop = measure(client, function)
    added = delayed - healthy
    print(f"  laptop round trip, for information: {healthy_laptop:.0f} -> "
          f"{delayed_laptop:.0f} ms, added {delayed_laptop - healthy_laptop:.0f} ms", flush=True)
    return report("delay +300 ms", 270 <= added <= 330,
                  f"dependency median {healthy:.1f} -> {delayed:.1f} ms, added {added:.1f} ms")


def check_outage(client, function):
    start_s, end_s = 10, 20
    run_start = int(time.time()) + 15   # leaves time for the configuration update
    print(f"outage: window {start_s}-{end_s} s after RUN_START, sampling about 40 s...", flush=True)
    set_faults(function, FAULT_MODE="outage", FAULT_OUTAGE_START_S=start_s,
               FAULT_OUTAGE_END_S=end_s, RUN_START=run_start, RUN_ID="ct5-outage")
    if time.time() > run_start + start_s - GUARD_S:
        return report("outage", False, "configuration update took too long; run again")

    inside, outside = [], []
    while time.time() < run_start + end_s + 10:
        outcome, sent_at, latency_ms = timed_call(client, 42)
        first = sent_at - run_start                # the dependency decided somewhere
        last = first + latency_ms / 1000           # between these two moments
        if first >= start_s + GUARD_S and last <= end_s - GUARD_S:
            inside.append(outcome)
        elif last <= start_s - GUARD_S or first >= end_s + GUARD_S:
            outside.append(outcome)

    inside_ok = len(inside) >= 20 and all(o == "injected" for o in inside)
    outside_ok = len(outside) >= 20 and all(o == "ok" for o in outside)
    return report("outage window", inside_ok and outside_ok,
                  f"inside {inside.count('injected')}/{len(inside)} failed, "
                  f"outside {outside.count('ok')}/{len(outside)} succeeded")


def main():
    function = tf_output("dependency_function_name")
    client = DependencyClient(tf_output("dependency_url"), REGION)
    checks = []
    try:
        checks.append(check_transient(client, function))
        checks.append(check_delay(client, function))
        checks.append(check_outage(client, function))
    finally:
        set_faults(function)
        print("dependency restored to healthy settings", flush=True)
    print("CT5:", "PASS" if all(checks) else "FAIL")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
