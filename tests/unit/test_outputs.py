"""Outputs 2 and 3 and the A/A noise floor, against values worked out by hand."""
import csv

import pytest

from analysis.outputs import (aa_pairs, c7_series, judge_effects, noise_floor, output2_tex,
                              write_outputs)


def run(experiment, configuration, condition, control=False, **metrics):
    return {"run_id": f"{experiment}-{condition}-{configuration}{'AA' if control else ''}",
            "experiment": experiment, "configuration": configuration, "condition": condition,
            "control": control, **metrics}


AA_ROWS = [  # two sessions, each with its regular Base C1 run and its A/A control
    run("s1", "Base", "C1", error_rate=0.0, p95_ms=50.0, cost_per_1000_usd=0.00185),
    run("s1", "Base", "C1", True, error_rate=0.0, p95_ms=46.0, cost_per_1000_usd=0.00186),
    run("s2", "Base", "C1", error_rate=0.0, p95_ms=44.0, cost_per_1000_usd=0.00185),
    run("s2", "Base", "C1", True, error_rate=0.0, p95_ms=47.0, cost_per_1000_usd=0.00185),
    run("s2", "Retry", "C1", error_rate=0.0, p95_ms=45.0, cost_per_1000_usd=0.00185),
]


def test_aa_pairs_and_noise_floor():
    pairs = aa_pairs(AA_ROWS)
    assert [(e, a["p95_ms"], b["p95_ms"]) for e, a, b in pairs] == [("s1", 50.0, 46.0),
                                                                    ("s2", 44.0, 47.0)]
    floor = noise_floor(pairs)
    # p95 gaps |50-46| = 4 and |44-47| = 3 -> 4; errors 0 and 0 -> 0;
    # cost gaps 0.00001 and 0 -> 0.00001: the floor is the largest gap.
    assert floor["p95_ms"] == 4.0 and floor["error_rate"] == 0.0
    assert floor["cost_per_1000_usd"] == pytest.approx(0.00001)
    assert noise_floor([]) == {"error_rate": None, "p95_ms": None, "cost_per_1000_usd": None}


def cell(condition, configuration, **values):
    out = {"condition": condition, "configuration": configuration}
    for metric in ("error_rate", "p95_ms", "cost_per_1000_usd"):
        out[f"{metric}_median"] = values.get(metric)
        out[f"{metric}_vs_base"] = values.get(f"{metric}_vs", "")
    return out


def test_effects_must_pass_the_range_rule_and_the_noise_floor():
    cells = [cell("C4", "Base", error_rate=0.10, p95_ms=47.0, cost_per_1000_usd=0.00202),
             cell("C4", "Retry", error_rate=0.0103, error_rate_vs="lower",
                  p95_ms=49.0, p95_ms_vs="higher",              # 2 ms <= floor 4 ms
                  cost_per_1000_usd=0.00192, cost_per_1000_usd_vs="inconclusive"),
             cell("C6", "Base", error_rate=1.0, p95_ms=1134.0),
             cell("C6", "Cache", error_rate=1.0, error_rate_vs="inconclusive",
                  p95_ms=1139.0, p95_ms_vs="inconclusive", cost_per_1000_usd_vs="n/a")]
    floor = {"error_rate": 0.0, "p95_ms": 4.0, "cost_per_1000_usd": 0.00001}
    judged = {(e["condition"], e["configuration"], e["metric"]): e
              for e in judge_effects(cells, floor)}
    retry_errors = judged[("C4", "Retry", "error_rate")]
    assert retry_errors["final"] == "lower"                  # 8.97 points above a floor of 0
    assert retry_errors["median_difference"] == pytest.approx(0.0103 - 0.10)
    assert judged[("C4", "Retry", "p95_ms")]["final"] == "below noise floor"
    assert judged[("C4", "Retry", "cost_per_1000_usd")]["final"] == "inconclusive"
    c6_cost = judged[("C6", "Cache", "cost_per_1000_usd")]
    assert (c6_cost["final"], c6_cost["median_difference"]) == ("n/a", None)
    assert ("C4", "Base", "error_rate") not in judged         # Base is not compared with itself


def test_output2_covers_c4_to_c7_with_the_predictions():
    def full(condition, configuration, expected=None):
        c = {"condition": condition, "configuration": configuration,
             "expected_read_error_rate": expected}
        for k, v in {"p50_ms": 31.6, "p95_ms": 46.6, "p99_ms": 55.0, "error_rate": 0.0998,
                     "read_error_rate": 0.1001, "throughput_rps": 45.0,
                     "mean_attempts": 1.0}.items():
            c[f"{k}_median"] = v
        return c
    cells = [full("C1", "Base"), full("C4", "Base", 0.1), full("C4", "Retry", 0.0001),
             full("C7", "Cache")]
    tex = output2_tex(cells)
    assert "C1 &" not in tex                                   # healthy load is not in RQ1
    assert "C4 & Base & 31.6 & 46.6 & 55.0 & 9.98 & 10.01 & 10.00 & 45.0 & 1.00 \\\\" in tex
    assert "C4 & Retry &" in tex and "& 0.01 &" in tex          # 0.1^4 = 0.01 %
    assert "C7 & Cache &" in tex and "& -- &" in tex            # no prediction with a cache


def write_buckets(folder, rows):
    folder.mkdir(parents=True)
    with open(folder / "buckets.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["index", "start_s", "requests", "failed",
                                               "error_rate", "p50_ms", "p95_ms", "p99_ms",
                                               "meets_slo"])
        writer.writeheader()
        for start, err, p99 in rows:
            writer.writerow({"index": start // 5, "start_s": start, "requests": 250,
                             "failed": int(err * 250), "error_rate": err, "p50_ms": 30,
                             "p95_ms": 40, "p99_ms": p99, "meets_slo": err <= 0.01})
    return folder


def test_c7_series_takes_the_median_per_bucket(tmp_path):
    results = tmp_path / "results"
    exp = results / "full-s1-1Z"
    exp.mkdir(parents=True)
    rows = [run("full-s1-1Z", "Base", "C7"), run("full-s1-1Z", "Base", "C7")]
    rows[1]["run_id"] += "-second"
    write_buckets(results / rows[0]["run_id"], [(55, 0.0, 50), (60, 1.0, 20), (65, 0.0, 60)])
    write_buckets(results / rows[1]["run_id"], [(55, 0.0, 52), (60, 1.0, 22), (65, 0.2, 80),
                                                (70, 0.0, 50)])
    series = c7_series(rows, [exp])
    # Medians of two runs: (0+0)/2, (1+1)/2, (0+0.2)/2 for errors; the 70 s bucket exists in
    # one run of two only, so it is left out.
    assert series["Base"] == [(55, 0.0, 51.0), (60, 1.0, 21.0), (65, 0.1, 70.0)]


def test_write_outputs_makes_every_file(tmp_path):
    exp = tmp_path / "full-s1-1Z"
    exp.mkdir()
    rows = [dict(r, experiment="full-s1-1Z") for r in AA_ROWS[:2]]
    c7 = run("full-s1-1Z", "Base", "C7")
    write_buckets(tmp_path / c7["run_id"], [(55, 0.0, 50), (60, 1.0, 20)])
    cells = [cell("C1", "Base", error_rate=0.0, p95_ms=50.0, cost_per_1000_usd=0.00185),
             cell("C1", "Cache", error_rate=0.0, p95_ms=45.0, p95_ms_vs="inconclusive",
                  cost_per_1000_usd=0.0016, cost_per_1000_usd_vs="lower")]
    for c in cells:
        for k in ("p50_ms", "p99_ms", "read_error_rate", "throughput_rps", "mean_attempts"):
            c[f"{k}_median"] = None
    info = write_outputs(rows + [c7], [exp], cells, tmp_path)
    assert info["pairs"] == 1 and info["floor"]["p95_ms"] == 4.0 and info["c7"]
    for name in ("output2_rq1.tex", "output3_c7.png", "output3_c7.pdf", "aa.csv", "aa.tex",
                 "effects.csv"):
        assert (tmp_path / name).exists(), name
    assert "Noise floor & \\multicolumn{2}{c}{0.00} & \\multicolumn{2}{c}{4.0}" in \
        (tmp_path / "aa.tex").read_text()
    effects = list(csv.DictReader(open(tmp_path / "effects.csv")))
    assert {(e["metric"], e["final"]) for e in effects} >= {("cost_per_1000_usd", "lower")}
