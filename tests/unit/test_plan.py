import json
from collections import Counter
from decimal import Decimal
import gzip
import random

import pytest

from orchestrate.logs_export import export_logs
from orchestrate.plan import CONDITIONS, fault_settings, load_plan, run_order, validate
from orchestrate.price_updater import choose_updates

FULL = {"name": "full", "seed": 5360, "repetitions": 5,
        "configurations": ["Base", "Retry", "Cache", "Retry+Cache"],
        "conditions": list(CONDITIONS)}


def test_full_plan_has_140_runs_each_cell_five_times():
    order = run_order(FULL)
    assert len(order) == 140
    assert set(Counter((r["configuration"], r["condition"]) for r in order).values()) == {5}


def test_each_repetition_is_one_shuffled_block_of_all_28_cells():
    order = run_order(FULL)
    for rep in range(5):
        block = order[rep * 28:(rep + 1) * 28]
        assert {r["repetition"] for r in block} == {rep + 1}
        assert len({(r["configuration"], r["condition"]) for r in block}) == 28
    assert order[:28] != order[28:56]                      # shuffled differently each block


def test_same_seed_same_order_other_seed_other_order():
    assert run_order(FULL) == run_order(FULL)
    assert run_order(FULL) != run_order({**FULL, "seed": 1})


@pytest.mark.parametrize("change", [
    {"configurations": ["Base", "Fast"]}, {"conditions": ["C9"]}, {"repetitions": 0},
    {"repetitions": True}, {"seed": "5360"}, {"configurations": []},
])
def test_bad_plans_are_rejected(change):
    with pytest.raises(ValueError):
        validate({**FULL, **change})


def test_plan_files_in_the_repository_are_valid():
    assert len(run_order(load_plan("experiments/smoke.json"))) == 1
    assert len(run_order(load_plan("experiments/pilot.json"))) == 8


def test_fault_settings_per_condition():
    c7 = fault_settings("C7", "run-1", 1_700_000_000.9)
    assert c7 == {"FAULT_MODE": "outage", "FAULT_OUTAGE_START_S": 60, "FAULT_OUTAGE_END_S": 120,
                  "RUN_ID": "run-1", "RUN_START": 1_700_000_000}
    assert fault_settings("C1", "run-2", 5) == {"RUN_ID": "run-2", "RUN_START": 5}
    assert fault_settings("C4", "r", 0)["FAULT_RATE"] == 0.10


def test_price_updates_change_ten_distinct_products_reproducibly():
    first = choose_updates(random.Random("5360-run"))
    assert first == choose_updates(random.Random("5360-run"))
    assert len(first) == 10 and len({p for p, _ in first}) == 10
    assert all(1 <= p <= 1000 and Decimal("5") <= price <= Decimal("500") for p, price in first)


class FakeLogs:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get_paginator(self, name):
        assert name == "filter_log_events"
        return self

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.get(kwargs["logGroupName"], [])


def test_log_export_keeps_every_event_of_every_group(tmp_path):
    event = {"timestamp": 1, "logStreamName": "s", "message": "REPORT RequestId: x\n"}
    logs = FakeLogs({"/a": [{"events": [event, event]}], "/b": [{"events": [event]}]})
    path = tmp_path / "logs.jsonl.gz"
    assert export_logs(logs, ["/a", "/b"], 0, 10, path) == 3
    lines = [json.loads(line) for line in gzip.open(path, "rt")]
    assert lines[0] == {"group": "/a", "t": 1, "stream": "s", "message": "REPORT RequestId: x"}
    assert logs.calls[0]["filterPattern"] == '?REPORT ?"type"'


def test_start_times_find_the_first_request_and_the_first_measured_one(tmp_path):
    from orchestrate.experiment import start_times
    path = tmp_path / "records.jsonl.gz"
    with gzip.open(path, "wt") as f:
        for t, ph in ((1005, "warmup"), (1000, "warmup"), (31002, "main"), (31000, "main")):
            f.write(json.dumps({"t": t, "ph": ph}) + "\n")
    assert start_times(path) == (1000, 31000)
