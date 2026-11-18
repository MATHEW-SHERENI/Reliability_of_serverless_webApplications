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


class FakeTable:
    def __init__(self):
        self.versions = {}

    def update_item(self, Key, **kwargs):
        pid = Key["product_id"]
        self.versions[pid] = self.versions.get(pid, 1) + 1
        return {"Attributes": {"version": Decimal(self.versions[pid])}}


def test_price_updater_thread_runs_rounds_and_stops_cleanly(tmp_path):
    from orchestrate.price_updater import PriceUpdater
    updater = PriceUpdater("unused", 5360, "run-1", interval_s=0.05, table=FakeTable())
    updater.start()
    import time
    time.sleep(0.18)
    updater.stop()                          # crashed on Python 3.12 before the fix
    assert not updater.is_alive()
    assert len(updater.log) >= 20 and len(updater.log) % 10 == 0 and updater.errors == 0
    updater.write_log(tmp_path / "versions.jsonl")
    assert len((tmp_path / "versions.jsonl").read_text().splitlines()) == len(updater.log)


def test_start_offset_uses_the_nov_14_smoke_run_numbers():
    from orchestrate.experiment import start_offset_s
    assert start_offset_s(1794719882, 1794719865944) == -16.1    # began 16 s early
    assert start_offset_s(100, None) is None


# ------------------------------------------------- sessions and the A/A control (Day 19)

from orchestrate.plan import estimated_seconds  # noqa: E402

WITH_CONTROL = {**FULL, "control": {"configuration": "Base", "condition": "C1"}}


def test_pilot_order_is_unchanged_by_the_new_options():
    # The order the pilot actually ran in on Nov 16 (experiment pilot-20261117T034701Z).
    ran = [("Cache", "C1"), ("Cache", "C4"), ("Retry+Cache", "C4"), ("Retry", "C4"),
           ("Retry", "C1"), ("Base", "C4"), ("Retry+Cache", "C1"), ("Base", "C1")]
    order = run_order(load_plan("experiments/pilot.json"))
    assert [(r["configuration"], r["condition"]) for r in order] == ran
    assert not any(r["control"] for r in order)


def test_control_adds_one_base_c1_run_to_every_block():
    order = run_order(WITH_CONTROL)
    assert len(order) == 145                                  # 5 blocks of 28 + 1
    for session in range(1, 6):
        block = [r for r in order if r["repetition"] == session]
        controls = [r for r in block if r["control"]]
        assert len(block) == 29 and len(controls) == 1
        assert (controls[0]["configuration"], controls[0]["condition"]) == ("Base", "C1")
        assert len({(r["configuration"], r["condition"]) for r in block if not r["control"]}) == 28
    regular = Counter((r["configuration"], r["condition"]) for r in order if not r["control"])
    assert set(regular.values()) == {5}                      # still five runs per cell


def test_a_session_is_exactly_its_block_of_the_whole_plan():
    whole = run_order(WITH_CONTROL)
    for session in range(1, 6):
        assert run_order(WITH_CONTROL, session) == [r for r in whole if r["repetition"] == session]


@pytest.mark.parametrize("session", [0, 6, "1", True])
def test_bad_sessions_are_rejected(session):
    with pytest.raises(ValueError):
        run_order(WITH_CONTROL, session)


@pytest.mark.parametrize("control", [
    {"configuration": "Fast", "condition": "C1"}, {"configuration": "Base", "condition": "C9"},
    {"configuration": "Base"}, "Base",
])
def test_bad_controls_are_rejected(control):
    with pytest.raises(ValueError):
        validate({**FULL, "control": control})


def test_full_and_shakedown_plan_files():
    full = load_plan("experiments/full.json")
    assert len(run_order(full)) == 145 and len(run_order(full, 1)) == 29
    # Session: 4 configurations x (5 x 210 s + 41 s + 330 s) + 210 s control = 5,894 s
    # of load, plus 29 x 60 s allowance = 7,634 s (about 2 h 7 min).
    assert estimated_seconds(run_order(full, 1)) == 7634
    shakedown = load_plan("experiments/shakedown.json")
    # 2 configurations x (41 + 330 + 3 x 210) s = 2,002 s, plus 10 x 60 s = 2,602 s.
    assert len(run_order(shakedown)) == 10 and estimated_seconds(run_order(shakedown)) == 2602
