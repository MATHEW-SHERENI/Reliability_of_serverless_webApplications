"""Smoke check (Day 05): call the deployed dependency through the real client."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))
from dep_client import DependencyClient, DependencyClientError  # noqa: E402

url = subprocess.run(["terraform", "-chdir=infra", "output", "-raw", "dependency_url"],
                     capture_output=True, text=True, check=True).stdout
client = DependencyClient(url, "us-east-2")

for call in range(1, 4):
    item = client.get_item(42, timeout_s=3.0)
    print(f"call {call}: product {item['product_id']}, stock {item['stock']}, "
          f"{client.last_latency_ms:.0f} ms")

try:
    client.reserve(42, 2_000_000, timeout_s=3.0)
except DependencyClientError as err:
    print(f"over-order rejected: {err} (status={err.status}, retryable={err.retryable})")
