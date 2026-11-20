"""The experiment summary: median and range per cell, the range rule, the C4 predictions."""
import csv

import pytest

from analysis.summarize import (compare, expected_rates, median_range, ranges_overlap,
                                summarise, summarise_experiment)

C4_ERRORS = {   # five runs per cell; medians and ranges worked out by hand below
    "Base": [0.10, 0.12, 0.08, 0.11, 0.09],     # sorted .08 .09 .10 .11 .12 -> 0.10 (0.08-0.12)
    "Retry": [0.010, 0.012, 0.011, 0.009, 0.013],   # -> 0.011 (0.009-0.013): no overlap, lower
    "Cache": [0.03, 0.035, 0.04, 0.05, 0.13],   # -> 0.04 (0.03-0.13): overlaps 0.08-0.12
}


def rows():
    out = []
    for configuration, errors in C4_ERRORS.items():
        for k, e in enumerate(errors):
            out.append({"run_id": f"{configuration}-{k}", "condition": "C4",
                        "configuration": configuration, "error_rate": e,
                        "p95_ms": 40.0 + k, "cost_per_1000_usd": 0.002,
                        "hit_ratio": None if configuration != "Cache" else 0.7})
    out.append({"run_id": "one", "condition": "C1", "configuration": "Base", "error_rate": 0.0,
                "p95_ms": 44.0, "cost_per_1000_usd": 0.00184, "hit_ratio": None})
    out.append({"run_id": "two", "condition": "C1", "configuration": "Cache", "error_rate": 0.0,
                "p95_ms": 43.0, "cost_per_1000_usd": 0.00159, "hit_ratio": 0.707})
    return out


def cell(cells, condition, configuration):
    return next(c for c in cells if (c["condition"], c["configuration"]) ==
                (condition, configuration))


def test_median_and_range():
    assert median_range([3, 1, 2]) == (2, 1, 3)
    assert median_range([1, 2, 3, 4]) == (2.5, 1, 4)          # even count: mean of the middle two
    assert median_range([None, 5, None]) == (5, 5, 5)
    assert median_range([None, None]) == (None, None, None)


def test_ranges_overlap_touching_counts():
    assert ranges_overlap((1, 2), (2, 3)) is True
    assert ranges_overlap((1, 2), (3, 4)) is False
    assert ranges_overlap((3, 4), (1, 2)) is False
    assert ranges_overlap((1, 5), (2, 3)) is True             # one inside the other


def test_cells_medians_ranges_and_verdicts():
    cells = summarise(rows())
    base, retry, cache = (cell(cells, "C4", n) for n in ("Base", "Retry", "Cache"))
    assert (base["runs"], base["error_rate_median"], base["error_rate_min"],
            base["error_rate_max"]) == (5, 0.10, 0.08, 0.12)
    assert retry["error_rate_median"] == pytest.approx(0.011)
    assert (retry["error_rate_min"], retry["error_rate_max"]) == (0.009, 0.013)
    assert retry["error_rate_vs_base"] == "lower"
    assert cache["error_rate_median"] == 0.04 and cache["error_rate_max"] == 0.13
    assert cache["error_rate_vs_base"] == "inconclusive"
    assert base["error_rate_vs_base"] == ""                   # Base is not compared with itself
    # p95 is 40-44 ms in every C4 cell: ranges overlap.
    assert retry["p95_ms_vs_base"] == "inconclusive"
    assert (base["hit_ratio_median"], base["hit_ratio_min"], base["hit_ratio_max"]) == \
        (None, None, None)
    # A metric missing from metrics.csv (an older file) gives None, not an error.
    assert base["read_error_rate_median"] is None


def test_one_run_per_cell_gives_no_verdict():
    cells = summarise(rows())
    c1_cache = cell(cells, "C1", "Cache")
    assert c1_cache["runs"] == 1 and c1_cache["error_rate_vs_base"] == "too few runs"
    assert compare({"runs": 5}, {"runs": 4}, "error_rate") == "too few runs"


def test_order_of_cells_follows_the_plan():
    cells = summarise(rows())
    assert [(c["condition"], c["configuration"]) for c in cells] == [
        ("C1", "Base"), ("C1", "Cache"), ("C4", "Base"), ("C4", "Retry"), ("C4", "Cache")]


def test_c4_predictions():
    assert expected_rates("C4", "Base") == (0.1, 0.1)
    read, order = expected_rates("C4", "Retry")
    assert read == pytest.approx(0.0001) and order == 0.1     # 0.1^4; orders never retried
    assert expected_rates("C4", "Cache") == (None, 0.1)       # depends on the hit ratio
    assert expected_rates("C1", "Base") == (None, None)


def test_summarise_experiment_writes_all_files(tmp_path):
    fields = sorted({k for r in rows() for k in r})
    with open(tmp_path / "metrics.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows():
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
    cells = summarise_experiment(tmp_path)
    assert len(cells) == 5
    summary = list(csv.DictReader(open(tmp_path / "summary.csv")))
    assert summary[3]["error_rate_vs_base"] == "lower"
    tex = (tmp_path / "summary.tex").read_text()
    assert r"\toprule" in tex and "C4 & Retry & 5 & 1.10 (0.90--1.30)" in tex
    assert "C1 & Cache & 1 & 0.00 & -- & --" in tex           # one run: median only
    assert (tmp_path / "comparison.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert (tmp_path / "comparison.pdf").read_bytes()[:5] == b"%PDF-"


def test_summarise_experiment_needs_metrics(tmp_path):
    with pytest.raises(FileNotFoundError, match="metrics.csv"):
        summarise_experiment(tmp_path)


def test_aa_control_runs_are_left_out_of_the_cells():
    extra = {"run_id": "aa", "condition": "C1", "configuration": "Base", "control": True,
             "error_rate": 0.5, "p95_ms": 999.0, "cost_per_1000_usd": 1.0, "hit_ratio": None}
    cells = summarise(rows() + [extra])
    c1_base = cell(cells, "C1", "Base")
    assert c1_base["runs"] == 1 and c1_base["p95_ms_median"] == 44.0


def test_several_sessions_are_combined(tmp_path):
    from analysis.summarize import summarise_experiments
    fields = sorted({k for r in rows() for k in r})
    for k, part in enumerate((rows()[:8], rows()[8:])):
        folder = tmp_path / f"s{k + 1}"
        folder.mkdir()
        with open(folder / "metrics.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in part:
                writer.writerow({key: ("" if r.get(key) is None else r.get(key)) for key in fields})
    cells = summarise_experiments([tmp_path / "s1", tmp_path / "s2"], tmp_path / "all")
    assert cell(cells, "C4", "Retry")["runs"] == 5           # split across the two folders
    assert cell(cells, "C4", "Retry")["error_rate_vs_base"] == "lower"
    assert (tmp_path / "all" / "comparison.png").exists()


def test_a_cell_without_successes_prints_and_plots(tmp_path):
    # C6: every request fails, so the cost per 1,000 successful requests is undefined.
    from analysis.summarize import console_table
    c6 = [{"run_id": f"c6-{k}", "condition": "C6", "configuration": "Base", "error_rate": 1.0,
           "p95_ms": 1140.0, "cost_per_1000_usd": None, "hit_ratio": None} for k in range(2)]
    cells = summarise(rows() + c6)
    lines = console_table(cells)
    c6_line = next(line for line in lines if line.startswith("C6 Base"))
    assert "100.00%" in c6_line and c6_line.rstrip().endswith("n/a")
    assert len(lines) == len(cells) + 1                       # a header and one line per cell
    fields = sorted({k for r in rows() + c6 for k in r})
    with open(tmp_path / "metrics.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows() + c6:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
    summarise_experiment(tmp_path)                            # figure and table, no error
    assert "C6 & Base & 2 & 100.00 (100.00--100.00)" in (tmp_path / "summary.tex").read_text()


# ------------------------------------------------- output 1: the SLO verdict matrix (Day 22)

def test_slo_matrix_pools_the_windows_of_all_runs(tmp_path):
    from analysis.summarize import plot_slo_matrix, share_text, slo_matrix_tex
    runs = [  # C7 Base: 2 of 3 minutes, twice; C7 Cache: 3 of 3 and 1 of 3; C1 Base: 3 of 3
        ("C7", "Base", 2, 3), ("C7", "Base", 2, 3), ("C7", "Cache", 3, 3),
        ("C7", "Cache", 1, 3), ("C1", "Base", 3, 3)]
    rows = [{"run_id": f"r{k}", "condition": cond, "configuration": conf,
             "slo_compliant_windows": ok, "slo_windows": n, "slo_compliant_share": ok / n,
             "error_rate": 0.0, "p95_ms": 40.0, "cost_per_1000_usd": 0.002}
            for k, (cond, conf, ok, n) in enumerate(runs)]
    cells = summarise(rows)
    c7_base, c7_cache = cell(cells, "C7", "Base"), cell(cells, "C7", "Cache")
    # Pooled: Base 4 of 6 windows = 2/3; Cache (3 + 1) of 6 = 2/3 as well, although its
    # two runs differ (range 1/3 to 1): the matrix shows the share, the range rule the spread.
    assert (c7_base["slo_compliant_total"], c7_base["slo_windows_total"]) == (4, 6)
    assert c7_base["slo_share_pooled"] == pytest.approx(2 / 3)
    assert c7_cache["slo_share_pooled"] == pytest.approx(2 / 3)
    assert (c7_cache["slo_compliant_share_min"], c7_cache["slo_compliant_share_max"]) == \
        pytest.approx((1 / 3, 1.0))
    assert c7_cache["slo_compliant_share_vs_base"] == "too few runs"
    assert share_text(c7_base) == "67 (4/6)" and share_text(None) == "--"
    tex = slo_matrix_tex(cells)
    # Columns: only the conditions present (C1, C7); rows: only configurations present.
    assert r"\textbf{Configuration} & \textbf{C1} & \textbf{C7} \\" in tex
    assert "Base & 100 (3/3) & 67 (4/6) \\\\" in tex
    assert "Cache & -- & 67 (4/6) \\\\" in tex               # Cache never ran under C1
    assert "Retry &" not in tex
    plot_slo_matrix(cells, [tmp_path / "m.png", tmp_path / "m.pdf"])
    assert (tmp_path / "m.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_runs_without_window_counts_give_no_share():
    cells = summarise([{"run_id": "x", "condition": "C1", "configuration": "Base",
                        "error_rate": 0.0}])
    assert cell(cells, "C1", "Base")["slo_share_pooled"] is None


def test_undefined_values_get_no_verdict():
    from analysis.summarize import compare
    cell_ = {"runs": 5, "cost_per_1000_usd_min": None, "cost_per_1000_usd_max": None}
    base = {"runs": 5, "cost_per_1000_usd_min": 0.002, "cost_per_1000_usd_max": 0.003}
    assert compare(cell_, base, "cost_per_1000_usd") == "n/a"     # C6: nothing succeeded
