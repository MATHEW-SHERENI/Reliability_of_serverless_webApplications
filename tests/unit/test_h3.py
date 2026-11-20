"""H3 analysis (task T4): breaker against no breaker in C7, values worked out by hand."""
import csv
import gzip
import json

from analysis.breaker import (degradation, h3_tex, judge_pairs, opening, outage_ms,
                              write_h3)


def run(configuration, k, condition="C7", experiment="e", control=False, **metrics):
    return {"run_id": f"{experiment}-{configuration}-{k}{'AA' if control else ''}",
            "experiment": experiment, "configuration": configuration,
            "condition": condition, "control": control, **metrics}


def record(env, t, lat, brk, ph="main"):
    return {"env": env, "t": t, "lat": lat, "brk": brk, "ph": ph, "st": 200}


# ------------------------------------------------- breakers per environment

def test_time_to_open_and_close_per_environment():
    start, end = 60_000, 120_000                    # C7: outage from 60 s to 120 s
    records = [
        record("a", 50_000, 10, "closed"),          # before the outage
        record("a", 61_000, 20, "closed"),
        record("a", 64_000, 500, "open"),           # open at 64.5 s: 4.5 s after the start
        record("a", 121_000, 0, "open"),            # still open after the outage
        record("a", 135_000, 0, "closed"),          # closed at 135 s: 15 s after the end
        record("b", 70_000, 0, "open"),             # 10 s after the start
        record("b", 125_000, 1_000, "closed"),      # answered at 126 s: 6 s after the end
        record("c", 100_000, 0, "closed"),          # served in the outage, never opened
        record("d", 10_000, 0, "closed"),           # only before the outage: not counted
        record("e", 80_000, 0, "open", ph="warmup"),          # warm-up: left out
        {"env": None, "t": 90_000, "lat": 10_000, "brk": None, "ph": "main", "st": 0},
    ]
    result = opening(records, start, end)
    # environments a, b, c; a and b opened: median(4.5, 10) = 7.25, max 10;
    # closed after median(15, 6) = 10.5, max 15
    assert result == {"environments": 3, "opened": 2, "never_opened": 1,
                      "median_open_s": 7.25, "max_open_s": 10.0,
                      "median_close_s": 10.5, "max_close_s": 15.0}


def test_outage_window_from_the_fault_schedule():
    manifest = {"faults": {"RUN_START": 1000, "FAULT_OUTAGE_START_S": 60,
                           "FAULT_OUTAGE_END_S": 120}}
    assert outage_ms(manifest) == (1_060_000, 1_120_000)      # (1000 + 60) s, (1000 + 120) s


# ------------------------------------------------- the pairs

def test_pairs_are_judged_by_ranges_and_the_noise_floor():
    rows = (
        [run("Cache", k, error_rate=e, p95_ms=p)
         for k, (e, p) in enumerate([(0.20, 44), (0.19, 43), (0.21, 45), (0.20, 44), (0.205, 46)])]
        + [run("Cache+Breaker", k, error_rate=e, p95_ms=p)
           for k, (e, p) in enumerate([(0.09, 43), (0.085, 42), (0.095, 44), (0.09, 43),
                                       (0.091, 44)])]
        + [run("Base", k, error_rate=0.333) for k in range(5)]
        + [run("Breaker", k, error_rate=e) for k, e in enumerate([0.336, 0.338, 0.34, 0.337,
                                                                  0.339])]
        + [run("Retry", k, error_rate=0.33) for k in range(5)]
        + [run("Retry+Breaker", k, error_rate=0.39) for k in range(4)]
        + [run("Cache+Breaker", 9, control=True, error_rate=0.9),       # A/A: left out
           run("Cache+Breaker", 8, condition="C1", error_rate=0.9)]     # not C7: left out
    )
    judged = {(j["with"], j["metric"]): j for j in judge_pairs(rows, {"error_rate": 0.01})}
    cache = judged[("Cache+Breaker", "error_rate")]
    # ranges 0.085-0.095 and 0.19-0.21 apart; medians 0.09 and 0.20: -0.11, beyond 0.01
    assert (cache["range_verdict"], cache["final"]) == ("lower", "lower")
    assert abs(cache["difference"] + 0.11) < 1e-12 and cache["runs_with"] == 5
    # p95: 42-44 against 43-46 overlap
    assert judged[("Cache+Breaker", "p95_ms")]["final"] == "inconclusive"
    # 0.336-0.34 against 0.333: apart, but medians 0.338 - 0.333 = 0.005 <= 0.01
    base = judged[("Breaker", "error_rate")]
    assert (base["range_verdict"], base["final"]) == ("higher", "below noise floor")
    assert judged[("Retry+Breaker", "error_rate")]["final"] == "too few runs"   # 4 runs
    assert judged[("Breaker", "p95_ms")]["final"] == "n/a"      # never measured here
    assert ("Retry+Cache+Breaker", "error_rate") not in judged  # neither side present


def test_table_marks_only_effects_that_pass():
    rows = ([run("Cache", k, error_rate=0.2, p95_ms=44) for k in range(5)]
            + [run("Cache+Breaker", k, error_rate=0.09, p95_ms=44) for k in range(5)])
    tex = h3_tex(judge_pairs(rows, {}))
    assert r"Cache+Breaker & 9.0\,$\downarrow$ & --" in tex      # 0.09 -> 9.0 %, lower
    assert r"Cache & 20.0 & --" in tex                          # the partner has no arrow
    assert tex.count(r"$\downarrow$") == 1                      # equal p95: no arrow


def test_degradation_per_breaker_configuration():
    rows = [run("Cache+Breaker", 0, degraded=300, fallback_reads=100, read_requests=1000,
                fallback_outdated=10),
            run("Cache+Breaker", 1, degraded=400, fallback_reads=150, read_requests=1000,
                fallback_outdated=30),
            run("Cache+Breaker", 2, degraded=200, fallback_reads=50, read_requests=1000,
                fallback_outdated=0)]
    openings = [{"configuration": "Cache+Breaker", "environments": n, "median_open_s": o,
                 "max_open_s": o + 1, "median_close_s": 12}
                for n, o in [(10, 5), (12, 6), (8, 7)]]
    (d,) = degradation(rows, openings)
    # fallback shares 0.10, 0.15, 0.05 -> 0.10; outdated 10/100, 30/150, 0/50 -> 0.10;
    # degraded 300, 400, 200 -> 300; environments 10, 12, 8 -> 10; open after 5, 6, 7 -> 6
    assert (d["fallback_share"], d["outdated_share"], d["degraded"]) == (0.10, 0.10, 300)
    assert (d["environments"], d["median_open_s"], d["max_open_s"]) == (10, 6, 7)


# ------------------------------------------------- everything from folders

def write_rows(path, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def make_run(results, row, records):
    folder = results / row["run_id"]
    folder.mkdir()
    write_rows(folder / "buckets.csv", [{"start_s": s, "requests": 10, "error_rate": e,
                                         "p99_ms": 40} for s, e in [(55, 0.0), (60, 1.0)]])
    (folder / "manifest.json").write_text(json.dumps({"faults": {
        "RUN_START": 0, "FAULT_OUTAGE_START_S": 60, "FAULT_OUTAGE_END_S": 120}}))
    with gzip.open(folder / "records.jsonl.gz", "wt") as f:
        f.write("".join(json.dumps(r) + "\n" for r in records))


def test_every_file_is_written(tmp_path):
    full, breaker = tmp_path / "full-s1-1Z", tmp_path / "breaker-2Z"
    full.mkdir()
    breaker.mkdir()
    full_rows = [run("Base", 0, "C1", "full-s1-1Z", error_rate=0.0),
                 run("Base", 1, "C1", "full-s1-1Z", True, error_rate=0.0),
                 run("Cache", 0, "C7", "full-s1-1Z", error_rate=0.2)]
    breaker_rows = [run("Cache+Breaker", 0, "C7", "breaker-2Z", error_rate=0.09, degraded=5,
                        fallback_reads=4, read_requests=8, fallback_outdated=1)]
    write_rows(full / "metrics.csv", full_rows)
    write_rows(breaker / "metrics.csv", breaker_rows)
    make_run(tmp_path, full_rows[2], [])
    make_run(tmp_path, breaker_rows[0], [record("x", 61_000, 0, "closed"),
                                         record("x", 63_000, 0, "open")])
    judged, degraded = write_h3([full], [breaker], tmp_path / "h3", tmp_path)
    for name in ("h3.csv", "h3.tex", "opening.csv", "degraded.tex", "h3_c7.png", "h3_c7.pdf",
                 "h3_opening.png", "h3_opening.pdf"):
        assert (tmp_path / "h3" / name).exists(), name
    opened = (tmp_path / "h3" / "opening.csv").read_text().splitlines()
    assert opened[1].startswith("breaker-2Z-Cache+Breaker-0,Cache+Breaker,1,1,0,3.0")  # 63 - 60
    assert degraded[0]["fallback_share"] == 0.5                                       # 4 of 8
    assert {j["final"] for j in judged} == {"too few runs"}                           # 1 run each
