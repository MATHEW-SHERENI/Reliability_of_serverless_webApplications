"""Run the k6 workload on the EC2 load host and collect the run's files.

The script travels through the results bucket: it is uploaded under
runs/<run_id>/, the load host is told through SSM to download it, run k6 while
recording CPU use with vmstat, and upload everything back to the same prefix.
The files are then copied to results/<run_id>/ on this computer.

Example (one full-size C1 run of whatever configuration is deployed):
    .venv/bin/python orchestrate/loadhost.py --condition C1 --config Base

The --config value is only a label; the orchestrator (M6) sets the real flags.
"""
import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
REGION = "us-east-2"
MAX_CPU_PERCENT = 70          # midterm: a run with load-host CPU above this is invalid
RUN_FILES = ("summary.json", "vmstat.log", "k6.out", "k6.err", "records.jsonl.gz")

REMOTE_SCRIPT = """set -euo pipefail
export HOME=/root AWS_DEFAULT_REGION={region}
cloud-init status --wait > /dev/null
ulimit -n 65535
WORK=/opt/b10/runs/{run_id}
rm -rf "$WORK"; mkdir -p "$WORK"; cd "$WORK"
aws s3 cp {prefix}workload.js workload.js --only-show-errors
vmstat -n 5 > vmstat.log &
VMSTAT_PID=$!
set +e
/usr/local/bin/k6 run --quiet --log-format=raw --console-output=records.jsonl {env_args} workload.js > k6.out 2> k6.err
K6_STATUS=$?
set -e
kill "$VMSTAT_PID" || true
gzip -f records.jsonl
aws s3 cp . {prefix} --recursive --only-show-errors
echo "k6 exit status $K6_STATUS"
exit "$K6_STATUS"
"""


def tf_output(name):
    return subprocess.run(["terraform", "-chdir=infra", "output", "-raw", name], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout


def remote_script(run_id, prefix, k6_env, region=REGION):
    """The shell commands the load host runs; every k6 variable is shell-quoted."""
    env_args = " ".join(f"-e {shlex.quote(f'{key}={value}')}" for key, value in k6_env.items())
    return REMOTE_SCRIPT.format(region=region, run_id=shlex.quote(run_id),
                                prefix=shlex.quote(prefix), env_args=env_args)


def max_cpu_percent(vmstat_text):
    """Highest CPU use (100 - idle) in a vmstat log, skipping the since-boot first line."""
    lines = [line.split() for line in vmstat_text.splitlines() if line.strip()]
    header = next(fields for fields in lines if "id" in fields)
    idle = header.index("id")
    samples = [fields for fields in lines if fields[0].isdigit()][1:]
    return max((100 - int(fields[idle]) for fields in samples), default=0)


def run_is_valid(summary, cpu_percent):
    """Midterm rule: no dropped k6 iterations and load-host CPU at most 70%."""
    return summary["dropped_iterations"] == 0 and cpu_percent <= MAX_CPU_PERCENT


def wait_until_online(ssm, instance_id, timeout_s=600):
    """Wait for the SSM agent on a freshly booted instance to report in."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])["InstanceInformationList"]
        if info and info[0]["PingStatus"] == "Online":
            return
        time.sleep(10)
    raise TimeoutError(f"{instance_id} did not come online in SSM within {timeout_s} s")


def run_command(ssm, instance_id, script, timeout_s):
    """Send a shell script through SSM and wait for it; returns the final invocation."""
    command_id = ssm.send_command(
        InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script], "executionTimeout": [str(timeout_s)]},
    )["Command"]["CommandId"]
    while True:
        time.sleep(5)
        try:
            invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            continue                          # not registered yet, right after sending
        if invocation["Status"] not in ("Pending", "InProgress", "Delayed"):
            return invocation


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--condition", default="C1")
    parser.add_argument("--config", default="Base", help="label only; flags are set elsewhere")
    parser.add_argument("--run-id")
    parser.add_argument("--seed", default="5360")
    parser.add_argument("--scale", default="1")
    parser.add_argument("--timeout-s", type=int, default=1200)
    args = parser.parse_args()

    run_id = args.run_id or f"{args.condition}-{args.config}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    bucket, instance_id = tf_output("results_bucket"), tf_output("loadhost_id")
    prefix_key = f"runs/{run_id}/"
    prefix = f"s3://{bucket}/{prefix_key}"
    k6_env = {"BASE_URL": tf_output("api_url"), "CONDITION": args.condition, "CONFIG": args.config,
              "RUN_ID": run_id, "SEED": args.seed, "SCALE": args.scale, "SUMMARY": "summary.json"}

    s3 = boto3.client("s3", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)

    print(f"run {run_id} on {instance_id}", flush=True)
    s3.upload_file(str(ROOT / "load" / "workload.js"), bucket, prefix_key + "workload.js")
    wait_until_online(ssm, instance_id)
    invocation = run_command(ssm, instance_id, remote_script(run_id, prefix, k6_env), args.timeout_s)
    print(f"SSM status: {invocation['Status']}; {invocation['StandardOutputContent'].strip()}")
    if invocation["Status"] != "Success":
        print(invocation["StandardErrorContent"][-2000:], file=sys.stderr)

    local_dir = ROOT / "results" / run_id
    local_dir.mkdir(parents=True, exist_ok=True)
    for name in RUN_FILES:
        s3.download_file(bucket, prefix_key + name, str(local_dir / name))

    summary = json.loads((local_dir / "summary.json").read_text())
    cpu = max_cpu_percent((local_dir / "vmstat.log").read_text())
    valid = run_is_valid(summary, cpu)
    print(f"requests {summary['requests']}, dropped iterations {summary['dropped_iterations']} "
          f"{summary.get('dropped_by_phase', '')}, max VUs {summary['max_vus']}, "
          f"load-host CPU max {cpu}%")
    print(f"run is {'VALID' if valid else 'INVALID'}; files in {local_dir}")
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
