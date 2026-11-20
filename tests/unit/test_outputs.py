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


# ------------------------------------------------- outputs 4 and 5 (Day 25)

from analysis.metrics import c3_steps  # noqa: E402
from analysis.outputs import c3_steps_by_config, output4_tex, output5_tex, plot_output4  # noqa: E402


def rec(t, lat, cache, env, cold="false", st=200, ph="main"):
    return {"t": t, "lat": lat, "cache": cache, "env": env, "cold": cold, "st": st, "ph": ph}


def test_c3_steps_by_hand():
    records = [rec(-10_000, 999, "HIT", "w", ph="warmup"),        # warm-up: left out
               rec(-500, 20, "HIT", "e1"),                         # just early: step 1
               rec(1_000, 10, "HIT", "e1", cold="true"),
               rec(30_000, 30, "MISS", "e2"),
               rec(59_000, 40, "MISS", "e2", st=503),
               rec(60_000, 100, "HIT", "e3"),
               rec(119_999, 200, "HIT", "e3"),
               rec(300_500, 50, "OFF", "e4")]                      # just late: step 5
    steps = c3_steps(records, 0)
    assert [s["target_rate"] for s in steps] == [100, 250, 500, 1000, 2000]
    one, two, three, _, five = steps
    # Step 1: latencies 20 10 30 40 -> p95 = 4th of 4 = 40; hits 2 of 4 cached reads;
    # one cold start; environments e1, e2; one failure of 4;
    # concurrency = 4 requests / 60 s x mean 25 ms = 0.0016667.
    assert (one["requests"], one["p95_ms"], one["hit_ratio"], one["cold_starts"],
            one["environments"], one["error_rate"]) == (4, 40, 0.5, 1, 2, 0.25)
    assert one["concurrency"] == pytest.approx(4 / 60 * 0.025)
    # Step 2: 2 requests, p95 = 200, both hits, one environment; 2/60 x 0.150 s.
    assert (two["requests"], two["p95_ms"], two["hit_ratio"], two["environments"]) == \
        (2, 200, 1.0, 1)
    assert two["concurrency"] == pytest.approx(2 / 60 * 0.150)
    assert (three["requests"], three["p95_ms"], three["hit_ratio"], three["concurrency"]) == \
        (0, None, None, None)
    assert (five["requests"], five["hit_ratio"], five["environments"]) == (1, None, 1)


def write_steps(folder, values):
    folder.mkdir(parents=True)
    with open(folder / "steps.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["step", "target_rate", "requests", "p95_ms",
                                               "error_rate", "hit_ratio", "cold_starts",
                                               "environments", "concurrency"])
        writer.writeheader()
        for k, (p95, hit, envs) in enumerate(values):
            writer.writerow({"step": k + 1, "target_rate": [100, 250][k], "requests": 6000,
                             "p95_ms": p95, "error_rate": 0, "hit_ratio": hit,
                             "cold_starts": 0, "environments": envs, "concurrency": 2.5})


def test_output4_medians_over_runs_and_files(tmp_path):
    exp = tmp_path / "full-s1-1Z"
    exp.mkdir()
    rows = [run("full-s1-1Z", "Cache", "C3"), run("full-s1-1Z", "Cache", "C3"),
            run("full-s1-1Z", "Cache", "C3")]
    for k, r in enumerate(rows):
        r["run_id"] += f"-{k}"
    write_steps(tmp_path / rows[0]["run_id"], [(50, 0.70, 3), (52, 0.74, 8)])
    write_steps(tmp_path / rows[1]["run_id"], [(48, 0.72, 4), (55, 0.76, 9)])
    write_steps(tmp_path / rows[2]["run_id"], [(60, 0.71, 3), (51, 0.75, 7)])
    steps = c3_steps_by_config(rows, [exp])
    # Medians of three runs: p95 (50, 48, 60) -> 50; hit (0.70, 0.72, 0.71) -> 0.71;
    # environments (3, 4, 3) -> 3; step 2: p95 (52, 55, 51) -> 52, environments 8.
    first, second = steps["Cache"]
    assert (first["p95_ms"], first["hit_ratio"], first["environments"], first["runs"]) == \
        (50, 0.71, 3, 3)
    assert (second["target_rate"], second["p95_ms"], second["environments"]) == (250, 52, 8)
    tex = output4_tex(steps)
    assert "Cache & 100 & 50.0 & 71.0 & 0 & 3 & 2.5 \\\\" in tex
    plot_output4(steps, [tmp_path / "o4.png"])
    assert (tmp_path / "o4.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_output5_cost_change_against_base_and_stale_rate():
    def c(condition, configuration, cost, stale=None):
        return {"condition": condition, "configuration": configuration,
                "cost_per_1000_usd_median": cost, "stale_rate_median": stale}
    cells = [c("C1", "Base", 0.00185), c("C1", "Cache", 0.00160, 0.021),
             c("C6", "Base", None), c("C6", "Cache", None, None)]
    tex = output5_tex(cells)
    # 0.00160 / 0.00185 - 1 = -13.5 % -> "-14%"; C6 has no successful request: "--".
    assert "Base & 1.85 & -- \\\\" in tex
    assert "Cache & 1.60 (-14\\%) & -- \\\\" in tex
    assert "Cache & 2.1 & -- \\\\" in tex                    # stale share of cache hits
    assert tex.count(r"\multicolumn{3}{@{}l}{\emph{") == 2    # the two row-group headings
