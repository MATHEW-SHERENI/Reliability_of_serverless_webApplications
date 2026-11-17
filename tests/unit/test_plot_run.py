"""The run figure: drawn from the files analysis/metrics.py writes, cost parts add up."""
import json

import pytest

from analysis.metrics import analyse_run, cost_parts, cost_usd
from analysis.plot_run import number, plot_run
from test_ct6_metrics import T0, fixture, write_gz


def test_cost_parts_add_up_to_the_cost():
    parts = cost_parts(20, 38, 1.555, 6.5, 8)
    assert list(parts) == ["API Gateway requests", "Lambda invocations", "Lambda compute (GB-s)",
                           "DynamoDB reads", "DynamoDB writes"]
    assert parts["API Gateway requests"] == pytest.approx(20e-6, rel=1e-12)
    assert sum(parts.values()) == pytest.approx(cost_usd(20, 38, 1.555, 6.5, 8), rel=1e-12)


def test_csv_text_becomes_values_again():
    assert [number(t) for t in ("", "None", "True", "False", "3", "0.25", "C7")] == \
        [None, None, True, False, 3, 0.25, "C7"]


def test_plot_run_writes_png_and_pdf(tmp_path):
    records, logs = fixture()
    run = tmp_path / "exp-001-C7-Base-r1-a1"
    run.mkdir()
    write_gz(run / "records.jsonl.gz", records)
    write_gz(run / "lambda_logs.jsonl.gz", logs)
    write_gz(run / "versions.jsonl.gz", [])
    faults = {"FAULT_MODE": "outage", "RUN_START": T0 // 1000 - 60,
              "FAULT_OUTAGE_START_S": 60, "FAULT_OUTAGE_END_S": 120}
    (run / "manifest.json").write_text(json.dumps({
        "run_id": run.name, "configuration": "Base", "condition": "C7", "repetition": 1,
        "attempt": 1, "valid": True, "faults": faults, "run_start": faults["RUN_START"]}))
    with pytest.raises(FileNotFoundError, match="metrics.csv"):
        plot_run(run)                                   # metrics first
    analyse_run(run)
    png, pdf = plot_run(run)
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and png.stat().st_size > 50_000
    assert pdf.read_bytes()[:5] == b"%PDF-"
