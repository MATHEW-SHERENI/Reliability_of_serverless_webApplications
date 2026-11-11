"""Day 12: the mechanism flags switch the deployed Catalog without a code change.

Integration test: needs `make deploy && make seed`. Run with `make test-integration`.
It makes the dependency fail every call, so the attempt count shows whether
retries are on, then checks a cache hit. It always restores the Base
configuration and a healthy dependency, so `make plan` shows no changes after.
"""
import subprocess

import pytest
import urllib3

from orchestrate.fault_config import set_faults, set_mechanisms

pytestmark = pytest.mark.integration


def tf_output(name):
    return subprocess.run(["terraform", "-chdir=infra", "output", "-raw", name],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module")
def stack():
    names = {"api": tf_output("api_url"),
             "catalog": tf_output("catalog_function_name"),
             "dependency": tf_output("dependency_function_name")}
    yield names
    set_mechanisms(names["catalog"], retry=False, cache=False)
    set_faults(names["dependency"])


def get(api, product_id=42):
    response = urllib3.PoolManager().request("GET", f"{api}products/{product_id}", timeout=10)
    return response.status, response.headers


def test_retry_flag_changes_the_attempt_count(stack):
    set_faults(stack["dependency"], FAULT_MODE="transient", FAULT_RATE=1.0, RUN_ID="day12")

    set_mechanisms(stack["catalog"], retry=False, cache=False)
    get(stack["api"])     # warm-up: a cold dependency can exceed the 1 s timeout (504)
    status, headers = get(stack["api"])
    assert status == 503 and headers["x-attempts"] == "1"

    set_mechanisms(stack["catalog"], retry=True, cache=False)
    get(stack["api"])     # warm-up for the new Catalog environment
    status, headers = get(stack["api"])
    assert status == 503 and headers["x-attempts"] == "4"


def test_cache_flag_serves_the_second_read_from_memory(stack):
    set_faults(stack["dependency"])
    set_mechanisms(stack["catalog"], retry=False, cache=True)
    get(stack["api"])                       # warm-up: may land on any environment
    hits = [get(stack["api"])[1]["x-cache"] for _ in range(5)]
    assert "HIT" in hits                    # the warmed environment answers from its cache
