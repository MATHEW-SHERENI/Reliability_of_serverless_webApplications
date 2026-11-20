"""Experiment orchestrator (M6): runs a plan file's runs and keeps the evidence.

For each run, in the plan's seeded, interleaved order:
  1. switch the Catalog's mechanism flags and the dependency's fault settings,
     and wait until AWS has applied them (every run starts new environments);
  2. start the price updater, run k6 on the load host (30 s warm-up except C2),
     and stop the updater;
  3. save the version log and the Lambda log lines next to the k6 files;
  4. judge the run: invalid if a file is missing, k6 dropped an iteration, or
     the load host's CPU went above 70%; an invalid run is queued again;
  5. write the run's manifest (commit, settings, times, result) to S3.
At the end, the metrics, a figure per run, the experiment's summary, and the
combined summary of all experiments of the same plan are produced (analysis/pipeline.py).
After the last run the deployed stack is put back to Base and a healthy
dependency, even if a run failed with an error.

Usage: .venv/bin/python orchestrate/experiment.py experiments/smoke.json
"""
import argparse
import gzip
import json
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrate.plan import (CONFIGURATIONS, estimated_seconds, fault_settings,  # noqa: E402
                              load_plan, run_order)

MAX_CPU_PERCENT = 70
MAX_ATTEMPTS = 3          # an invalid run is queued again at most twice
STARTUP_S = 5             # time from setting RUN_START until k6 starts on the load host;
                          # measured on Nov 14: about 4 s (the first estimate, 20 s, was too long)
WARMUP_S = 30


def judge(result):
    """(valid, reason) for one run's result, by the midterm's validity rules."""
    if result.get("missing_files"):
        return False, f"missing run files: {', '.join(result['missing_files'])}"
    summary, cpu = result.get("summary"), result.get("cpu_max")
    if summary is None or cpu is None:
        return False, "no k6 summary or CPU record"
    if summary["dropped_iterations"] > 0:
        return False, f"{summary['dropped_iterations']} dropped iterations"
    if cpu > MAX_CPU_PERCENT:
        return False, f"load-host CPU {cpu}% above {MAX_CPU_PERCENT}%"
    return True, "valid"


def valid_runs(manifests):
    """The runs the analysis may use: valid ones only, one per plan cell and repetition.

    The A/A control run is kept apart from the regular run of the same cell.
    """
    chosen = {}
    for m in manifests:
        if m["valid"]:
            key = (m["configuration"], m["condition"], m["repetition"], bool(m.get("control")))
            chosen[key] = m
    return list(chosen.values())


def start_times(records_path):
    """Send time (ms) of the first request, and of the first measured-phase request."""
    first = first_main = None
    with gzip.open(records_path, "rt") as f:
        for line in f:
            if not line.startswith("{"):
                continue
            record = json.loads(line)
            first = record["t"] if first is None else min(first, record["t"])
            if record["ph"] == "main":
                first_main = record["t"] if first_main is None else min(first_main, record["t"])
    return first, first_main


def start_offset_s(run_start, measured_start_ms):
    """How far the measured phase began after the planned RUN_START (negative = before)."""
    if measured_start_ms is None:
        return None
    return round(measured_start_ms / 1000 - run_start, 1)


def run_id_for(experiment_id, number, cell, attempt):
    config = cell["configuration"].replace("+", "") + ("AA" if cell.get("control") else "")
    return f"{experiment_id}-{number:03d}-{cell['condition']}-{config}-r{cell['repetition']}-a{attempt}"


def run_experiment(plan, backend, experiment_id, max_attempts=MAX_ATTEMPTS, say=print,
                   session=None):
    """Run every cell of the plan (or of one session); returns every attempt's manifest."""
    queue = deque((cell, 1) for cell in run_order(plan, session))
    manifests, number = [], 0
    try:
        while queue:
            cell, attempt = queue.popleft()
            number += 1
            run_id = run_id_for(experiment_id, number, cell, attempt)
            say(f"[{number}] {run_id}")
            manifest = {"experiment": experiment_id, "run_id": run_id, "attempt": attempt,
                        **cell, "seed": plan["seed"], "started_utc": _utc()}
            try:
                result = backend.execute(cell, run_id, plan)
                valid, reason = judge(result)
            except Exception as err:            # one broken run must not end the experiment
                result, valid, reason = {}, False, f"error: {err!r}"
            manifest.update(result, valid=valid, reason=reason, ended_utc=_utc())
            try:
                backend.save_manifest(manifest)
            except Exception as err:            # evidence that cannot be stored = no evidence
                manifest.update(valid=False, reason=f"manifest not saved: {err!r}")
            manifests.append(manifest)
            say(f"    {'VALID' if manifest['valid'] else 'INVALID'}: {manifest['reason']}")
            if not manifest["valid"]:
                if attempt < max_attempts:
                    queue.append((cell, attempt + 1))
                    say("    queued again")
                else:
                    say(f"    given up after {attempt} attempts")
    finally:
        backend.restore()
    backend.save_experiment(experiment_id, plan, manifests)
    return manifests


def _utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AwsBackend:
    """The real cloud: Lambda configuration, the load host, S3 and CloudWatch Logs."""

    def __init__(self, region="us-east-2"):
        import boto3

        from orchestrate.fault_config import set_faults, set_mechanisms
        from orchestrate.loadhost import run_on_loadhost, tf_output
        self.set_faults, self.set_mechanisms = set_faults, set_mechanisms
        self.run_on_loadhost = run_on_loadhost
        self.region = region
        out = {name: tf_output(name) for name in (
            "api_url", "catalog_function_name", "dependency_function_name",
            "products_table", "results_bucket", "loadhost_id", "loadhost_ami")}
        self.out = out
        self.s3 = boto3.client("s3", region_name=region)
        self.ssm = boto3.client("ssm", region_name=region)
        self.logs = boto3.client("logs", region_name=region)
        self.commit, self.dirty = _git_state()

    def execute(self, cell, run_id, plan):
        from orchestrate.logs_export import export_logs
        from orchestrate.price_updater import PriceUpdater

        out, condition = self.out, cell["condition"]
        retry, cache = CONFIGURATIONS[cell["configuration"]]
        warmup = 0 if condition == "C2" else WARMUP_S
        self.set_mechanisms(out["catalog_function_name"], retry, cache,
                            region=self.region, run_id=run_id)
        run_start = time.time() + STARTUP_S + warmup       # when the measured phase begins
        faults = fault_settings(condition, run_id, run_start)
        self.set_faults(out["dependency_function_name"], region=self.region, **faults)

        k6_env = {"BASE_URL": out["api_url"], "CONDITION": condition,
                  "CONFIG": cell["configuration"], "RUN_ID": run_id, "SEED": str(plan["seed"]),
                  "SCALE": str(plan.get("scale", 1)), "SUMMARY": "summary.json"}
        local_dir = ROOT / "results" / run_id
        updater = PriceUpdater(out["products_table"], plan["seed"], run_id, region=self.region)
        sent_ms = int(time.time() * 1000)
        updater.start()
        try:
            result = self.run_on_loadhost(self.s3, self.ssm, out["results_bucket"],
                                          out["loadhost_id"], run_id, k6_env, local_dir)
        finally:
            updater.stop()
        updater.write_log(local_dir / "versions.jsonl")
        first_ms = main_ms = None
        if (local_dir / "records.jsonl.gz").exists():
            first_ms, main_ms = start_times(local_dir / "records.jsonl.gz")

        time.sleep(15)                                      # log lines arrive a few seconds late
        groups = [f"/aws/lambda/{out['catalog_function_name']}",
                  f"/aws/lambda/{out['dependency_function_name']}"]
        log_lines = export_logs(self.logs, groups, sent_ms - 10_000, time.time() * 1000,
                                local_dir / "lambda_logs.jsonl.gz")
        _gzip(local_dir / "versions.jsonl")
        prefix = f"runs/{run_id}/"
        for name in ("versions.jsonl.gz", "lambda_logs.jsonl.gz"):
            self.s3.upload_file(str(local_dir / name), out["results_bucket"], prefix + name)

        return {**result, "mechanisms": {"MECH_RETRY": retry, "MECH_CACHE": cache},
                "faults": faults, "run_start": int(run_start), "k6_sent_ms": sent_ms,
                "first_request_ms": first_ms, "measured_start_ms": main_ms,
                "start_offset_s": start_offset_s(run_start, main_ms),
                "price_updates": len(updater.log), "price_update_errors": updater.errors,
                "lambda_log_lines": log_lines, "git_commit": self.commit,
                "git_dirty": self.dirty, "loadhost_id": out["loadhost_id"],
                "loadhost_ami": out["loadhost_ami"]}

    def save_manifest(self, manifest):
        body = json.dumps(manifest, indent=2, default=str)
        local = ROOT / "results" / manifest["run_id"]
        local.mkdir(parents=True, exist_ok=True)
        (local / "manifest.json").write_text(body)
        self.s3.put_object(Bucket=self.out["results_bucket"],
                           Key=f"runs/{manifest['run_id']}/manifest.json", Body=body.encode())

    def save_experiment(self, experiment_id, plan, manifests):
        record = {"experiment": experiment_id, "plan": plan, "git_commit": self.commit,
                  "git_dirty": self.dirty, "runs": manifests,
                  "valid_runs": [m["run_id"] for m in valid_runs(manifests)]}
        body = json.dumps(record, indent=2, default=str)
        local = ROOT / "results" / experiment_id
        local.mkdir(parents=True, exist_ok=True)
        (local / "experiment.json").write_text(body)
        self.s3.put_object(Bucket=self.out["results_bucket"],
                           Key=f"experiments/{experiment_id}/experiment.json", Body=body.encode())

    def restore(self):
        self.set_mechanisms(self.out["catalog_function_name"], False, False, region=self.region)
        self.set_faults(self.out["dependency_function_name"], region=self.region)


def _git_state():
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))


def _gzip(path):
    with open(path, "rb") as src, gzip.open(f"{path}.gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


def describe(plan, session=None, say=print):
    """Print the runs and the estimated duration of a plan or session, without AWS."""
    runs = run_order(plan, session)
    minutes = estimated_seconds(runs) / 60
    where = f"session {session} of {plan['repetitions']}" if session else "whole plan"
    say(f"{plan['name']}, {where}: {len(runs)} runs, about {minutes // 60:.0f} h "
        f"{minutes % 60:.0f} min")
    for number, cell in enumerate(runs, 1):
        say(f"  {number:3d}  {cell['condition']}  {cell['configuration']:12}"
            f"{'  A/A control' if cell.get('control') else ''}")
    return runs


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", help="plan file, e.g. experiments/smoke.json")
    parser.add_argument("--session", type=int, help="run only this repetition's block (1, 2, ...)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the run order and the estimated time; touch nothing")
    args = parser.parse_args()
    plan = load_plan(args.plan)
    runs = describe(plan, args.session, say=print if args.dry_run else lambda *a: None)
    if args.dry_run:
        return 0
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    session = f"-s{args.session}" if args.session else ""
    experiment_id = f"{plan['name']}{session}-{stamp}"
    backend = AwsBackend()
    if backend.dirty:
        print("warning: uncommitted changes; the manifest records the commit as dirty")
    print(f"experiment {experiment_id}: {len(runs)} runs, about "
          f"{estimated_seconds(runs) / 60:.0f} min, commit {backend.commit[:7]}")
    manifests = run_experiment(plan, backend, experiment_id, session=args.session)
    kept = valid_runs(manifests)
    print(f"done: {len(kept)} of {len(runs)} planned runs valid, "
          f"{len(manifests)} attempts; manifest in results/{experiment_id}/experiment.json")
    # Figures and tables straight away: metrics, one figure per run, the session's
    # summary, and the combined summary of every experiment of this plan so far.
    from analysis.pipeline import after_experiment
    after_experiment(experiment_id, plan["name"])
    return 0 if len(kept) == len(runs) else 1


if __name__ == "__main__":
    sys.exit(main())
