"""Task T4, hypothesis H3: the circuit breaker in the sustained outage C7.

The breaker experiment (experiments/breaker.json) ran the four configurations
with the breaker in C7. The full experiment ran the same four without it, with
the same seed, workload and outage, so each breaker configuration is compared
with its partner: Base and Breaker, Retry and Retry+Breaker, and so on.

H3 (midterm Section 2): in a sustained outage, an open breaker returns fallback
responses at near warm-path latency instead of waiting for timeouts; recovery
is bounded by its 15 s cooldown; and because breaker state is per environment,
the time to open grows with concurrency.

Written into results/h3/ (make h3):
  h3.csv            every pair and metric: median and range without and with the
                    breaker, the range verdict, and the verdict after the noise floor
  h3.tex            the same medians as a table, arrows where the breaker made a
                    difference that passes both rules
  opening.csv       per breaker run: environments that served requests during the
                    outage, how long their breakers took to open and to close again
  degraded.tex      per breaker configuration: environments, time to open and to
                    close, degraded responses, and reads served by the stale fallback
  h3_c7.png, .pdf   error rate and p99 every 5 s through the outage, one panel per
                    pair, without (dashed) and with the breaker (solid)
  h3_opening.png    median time to open against environments, one point per run

Rules as for the full experiment: an effect counts only if the two ranges of five
runs do not overlap, and, for the three outcomes the A/A runs measure, only if
the difference of the medians is larger than the noise floor.
"""
import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.metrics import read_jsonl  # noqa: E402
from analysis.outputs import COLOURS, aa_pairs, c7_series, noise_floor  # noqa: E402
from analysis.plot_run import INK, latency_axis, plt, read_csv, style  # noqa: E402
from analysis.summarize import MIN_RUNS, median_range, ranges_overlap  # noqa: E402
from orchestrate.plan import CONDITIONS  # noqa: E402

PAIRS = (("Base", "Breaker"), ("Retry", "Retry+Breaker"), ("Cache", "Cache+Breaker"),
         ("Retry+Cache", "Retry+Cache+Breaker"))
H3_METRICS = ("error_rate", "read_error_rate", "order_error_rate", "p95_ms", "p99_ms",
              "mean_attempts", "recovery_s", "cost_per_1000_usd")
OUTAGE_S = (CONDITIONS["C7"]["FAULT_OUTAGE_START_S"], CONDITIONS["C7"]["FAULT_OUTAGE_END_S"])
RESULTS = ROOT / "results"


# ------------------------------------------------------------------ the comparison

def c7_rows(rows):
    """The C7 runs that count: A/A control runs are left out, as in the summary."""
    return [r for r in rows if r["condition"] == "C7" and not r.get("control")]


def verdict(without, with_):
    """Range rule between two cells (lists of per-run values): lower, higher,
    inconclusive, n/a (a value undefined), or too few runs."""
    if len(without) < MIN_RUNS or len(with_) < MIN_RUNS:
        return "too few runs"
    if None in without or None in with_:
        return "n/a"
    a, b = (min(with_), max(with_)), (min(without), max(without))
    if ranges_overlap(a, b):
        return "inconclusive"
    return "lower" if a[1] < b[0] else "higher"


def judge_pairs(rows, floor):
    """One row per pair and metric: median (range) without and with the breaker, the
    range verdict, the difference of the medians, and the final verdict.

    The final verdict is "below noise floor" when the ranges are apart but the
    medians differ by no more than two identical Base runs did (A/A).
    """
    rows = c7_rows(rows)
    judged = []
    for without_name, with_name in PAIRS:
        without = [r for r in rows if r["configuration"] == without_name]
        with_ = [r for r in rows if r["configuration"] == with_name]
        if not without or not with_:
            continue
        for metric in H3_METRICS:
            a = [r.get(metric) for r in without]
            b = [r.get(metric) for r in with_]
            (ma, lo_a, hi_a), (mb, lo_b, hi_b) = median_range(a), median_range(b)
            difference = None if ma is None or mb is None else mb - ma
            range_verdict = verdict(a, b)
            final = range_verdict
            if range_verdict in ("lower", "higher") and floor.get(metric) is not None \
                    and abs(difference) <= floor[metric]:
                final = "below noise floor"
            judged.append({"without": without_name, "with": with_name, "metric": metric,
                           "runs_without": len(a), "runs_with": len(b),
                           "median_without": ma, "min_without": lo_a, "max_without": hi_a,
                           "median_with": mb, "min_with": lo_b, "max_with": hi_b,
                           "difference": difference, "range_verdict": range_verdict,
                           "noise_floor": floor.get(metric), "final": final})
    return judged


# ------------------------------------------------------------------ breakers per environment

def outage_ms(manifest):
    """(start, end) of the injected outage in epoch ms, from the run's fault schedule."""
    faults = manifest.get("faults") or {}
    start = int(faults["RUN_START"])
    return ((start + int(faults["FAULT_OUTAGE_START_S"])) * 1000,
            (start + int(faults["FAULT_OUTAGE_END_S"])) * 1000)


def opening(records, start_ms, end_ms):
    """How the breakers of one run behaved, environment by environment.

    An environment counts if it answered a measured request sent during the
    outage. Its breaker opened at the first answer (send time + latency) at or
    after the outage start that reported X-Breaker: open; it closed again at the
    first answer at or after the outage end that reported closed. Requests without
    an answer carry no headers and say nothing about the breaker.

    Returns environments, opened, never_opened, and the median and maximum
    seconds to open (from the outage start) and to close (from the outage end).
    """
    measured = [r for r in records if r.get("ph") != "warmup" and r.get("env")]
    envs = sorted({r["env"] for r in measured if start_ms <= r["t"] < end_ms})
    to_open, to_close = [], []
    for env in envs:
        mine = sorted((r["t"] + r["lat"], r.get("brk")) for r in measured if r["env"] == env)
        opened = next((t for t, state in mine if t >= start_ms and state == "open"), None)
        if opened is None:
            continue
        to_open.append((opened - start_ms) / 1000)
        closed = next((t for t, state in mine if t >= end_ms and state == "closed"), None)
        if closed is not None:
            to_close.append((closed - end_ms) / 1000)
    return {"environments": len(envs), "opened": len(to_open),
            "never_opened": len(envs) - len(to_open),
            "median_open_s": statistics.median(to_open) if to_open else None,
            "max_open_s": max(to_open) if to_open else None,
            "median_close_s": statistics.median(to_close) if to_close else None,
            "max_close_s": max(to_close) if to_close else None}


def opening_rows(rows, results_dir):
    """opening() for every breaker run in rows (C7, configuration with a breaker)."""
    out = []
    for r in c7_rows(rows):
        if "Breaker" not in r["configuration"]:
            continue
        run_dir = Path(results_dir) / r["run_id"]
        manifest = json.loads((run_dir / "manifest.json").read_text())
        start, end = outage_ms(manifest)
        out.append({"run_id": r["run_id"], "configuration": r["configuration"],
                    **opening(read_jsonl(run_dir / "records.jsonl.gz"), start, end)})
    return out


def degradation(rows, openings):
    """Per breaker configuration, medians over its runs: environments in the outage,
    seconds to open and to close, degraded responses, the share of reads served by
    the stale fallback, and the share of those that were outdated."""
    rows = c7_rows(rows)
    out = []
    for _, name in PAIRS:
        runs = [r for r in rows if r["configuration"] == name]
        mine = [o for o in openings if o["configuration"] == name]
        if not runs:
            continue

        def med(values):
            return median_range(values)[0]
        out.append({
            "configuration": name, "runs": len(runs),
            "environments": med([o["environments"] for o in mine]),
            "median_open_s": med([o["median_open_s"] for o in mine]),
            "max_open_s": med([o["max_open_s"] for o in mine]),
            "median_close_s": med([o["median_close_s"] for o in mine]),
            "degraded": med([r.get("degraded") for r in runs]),
            "fallback_share": med([r["fallback_reads"] / r["read_requests"]
                                   for r in runs if r.get("read_requests")]),
            "outdated_share": med([r["fallback_outdated"] / r["fallback_reads"]
                                   for r in runs if r.get("fallback_reads")]),
        })
    return out


# ------------------------------------------------------------------ tables

def fmt(value, digits=1, scale=1):
    return "--" if value is None else f"{value * scale:,.{digits}f}"


TABLE_COLUMNS = (("error_rate", 1, 100), ("read_error_rate", 1, 100),
                 ("order_error_rate", 1, 100), ("p95_ms", 0, 1), ("p99_ms", 0, 1),
                 ("mean_attempts", 2, 1), ("recovery_s", 0, 1), ("cost_per_1000_usd", 2, 1000))


def h3_tex(judged):
    """Medians without and with the breaker; an arrow marks a difference that passes
    the range rule (and the noise floor, where one exists)."""
    by = {(j["with"], j["metric"]): j for j in judged}
    lines = [r"\begin{tabular}{@{}lrrrrrrrr@{}}", r"\toprule",
             r"\textbf{Configuration} & \textbf{Errors} & \textbf{Reads} & \textbf{Orders} & "
             r"\textbf{p95} & \textbf{p99} & $\bar{a}$ & $R$ & \textbf{\$/1{,}000} \\",
             r" & (\%) & (\%) & (\%) & (ms) & (ms) & & (s) & ($10^{-3}$) \\", r"\midrule"]
    first = True
    for without_name, with_name in PAIRS:
        if not any(j["with"] == with_name for j in judged):
            continue
        if not first:
            lines.append(r"\midrule")
        first = False
        for name, side in ((without_name, "without"), (with_name, "with")):
            cells = []
            for metric, digits, scale in TABLE_COLUMNS:
                j = by.get((with_name, metric))
                text = fmt(None if j is None else j[f"median_{side}"], digits, scale)
                if side == "with" and j is not None and j["final"] in ("lower", "higher"):
                    text += r"\,$\downarrow$" if j["final"] == "lower" else r"\,$\uparrow$"
                cells.append(text)
            lines.append(name + " & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", ""]
    return "\n".join(lines)


def degraded_tex(rows):
    lines = [r"\begin{tabular}{@{}lrrrrrr@{}}", r"\toprule",
             r"\textbf{Configuration} & \textbf{Environments} & \textbf{Open after} & "
             r"\textbf{Last open} & \textbf{Closed after} & \textbf{Fallback reads} & "
             r"\textbf{Outdated} \\",
             r" & & (s) & (s) & (s) & (\% of reads) & (\% of fallbacks) \\", r"\midrule"]
    for d in rows:
        lines.append(" & ".join([d["configuration"], fmt(d["environments"], 0),
                                 fmt(d["median_open_s"]), fmt(d["max_open_s"]),
                                 fmt(d["median_close_s"]), fmt(d["fallback_share"], 1, 100),
                                 fmt(d["outdated_share"], 1, 100)]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ figures

def plot_h3_series(series, paths):
    """Two rows (error rate, p99) by four pairs; the outage shaded."""
    style()
    fig, axes = plt.subplots(2, len(PAIRS), figsize=(13, 6.5), sharex=True, sharey="row",
                             layout="constrained")
    every = [p[2] for points in series.values() for p in points] or [100]
    for k, (without_name, with_name) in enumerate(PAIRS):
        top, bottom = axes[0][k], axes[1][k]
        for ax in (top, bottom):
            ax.axvspan(*OUTAGE_S, color="#e4e3df", alpha=0.7, linewidth=0)
        for name, dash, label in ((without_name, "--", "without breaker"),
                                  (with_name, "-", "with breaker")):
            points = series.get(name)
            if not points:
                continue
            x = [p[0] + 2.5 for p in points]             # middle of each 5 s bucket
            top.plot(x, [p[1] * 100 for p in points], dash, color=COLOURS[name],
                     linewidth=1.6, label=label)
            bottom.plot(x, [p[2] for p in points], dash, color=COLOURS[name], linewidth=1.6)
        top.set_title(f"{without_name} / {with_name}", fontsize=9, loc="left", color=INK)
        top.set_ylim(0, 105)
        if top.get_lines():
            top.legend(fontsize=8, frameon=False, loc="upper right")
        latency_axis(bottom, every)
        bottom.set_ylabel("")                    # one label per row, on the left panel
        bottom.set_xlabel("Seconds from the planned start")
    axes[0][0].set_ylabel("Error rate (%)")
    axes[1][0].set_ylabel("p99 latency (ms)")
    for path in paths:
        fig.savefig(path, dpi=200)
    plt.close(fig)


def plot_opening(openings, paths):
    """Median seconds to open against environments in the outage, one point per run."""
    style()
    fig, ax = plt.subplots(figsize=(6.5, 4.2), layout="constrained")
    for _, name in PAIRS:
        points = [o for o in openings if o["configuration"] == name
                  and o["median_open_s"] is not None]
        if points:
            ax.plot([o["environments"] for o in points], [o["median_open_s"] for o in points],
                    "o", markersize=6, color=COLOURS[name], label=name,
                    markeredgecolor="white", markeredgewidth=1)
    ax.set_xlabel("Execution environments serving requests during the outage")
    ax.set_ylabel("Median time to open (s)")
    ax.set_ylim(bottom=0)
    if ax.get_lines():
        ax.legend(fontsize=8, frameon=False, loc="best")
    for path in paths:
        fig.savefig(path, dpi=200)
    plt.close(fig)


# ------------------------------------------------------------------ all together

def write_csv(path, rows):
    if not rows:
        Path(path).write_text("")
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_h3(full_dirs, breaker_dirs, out_dir, results_dir=RESULTS):
    """Read the metrics of both experiments, write every H3 table and figure into
    out_dir, and return (judged, degradation) for the console."""
    full = [r for d in full_dirs for r in read_csv(Path(d) / "metrics.csv")]
    breaker = [r for d in breaker_dirs for r in read_csv(Path(d) / "metrics.csv")]
    floor = noise_floor(aa_pairs(full))
    rows = c7_rows(full) + c7_rows(breaker)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    judged = judge_pairs(rows, floor)
    write_csv(out_dir / "h3.csv", judged)
    (out_dir / "h3.tex").write_text(h3_tex(judged))
    openings = opening_rows(breaker, results_dir)
    write_csv(out_dir / "opening.csv", openings)
    degraded = degradation(rows, openings)
    (out_dir / "degraded.tex").write_text(degraded_tex(degraded))
    series = c7_series(rows, [str(d) for d in list(full_dirs) + list(breaker_dirs)])
    plot_h3_series(series, [out_dir / "h3_c7.png", out_dir / "h3_c7.pdf"])
    plot_opening(openings, [out_dir / "h3_opening.png", out_dir / "h3_opening.pdf"])
    return judged, degraded


def console(judged, degraded):
    """Short lines for the terminal: each pair's verdicts, then the breakers."""
    lines = []
    for without_name, with_name in PAIRS:
        mine = [j for j in judged if j["with"] == with_name]
        if mine:
            lines.append(f"{without_name} -> {with_name}")
            for j in mine:
                lines.append(f"  {j['metric']:18s} {fmt(j['median_without'], 4):>10s} -> "
                             f"{fmt(j['median_with'], 4):>10s}   {j['final']}")
    lines.append("breakers (medians over runs): environments, open after s, closed after s, "
                 "fallback reads % of reads")
    for d in degraded:
        lines.append(f"  {d['configuration']:20s} {fmt(d['environments'], 0):>5s} "
                     f"{fmt(d['median_open_s']):>6s} {fmt(d['median_close_s']):>6s} "
                     f"{fmt(d['fallback_share'], 1, 100):>6s}")
    return lines


def main():
    from analysis.pipeline import experiments
    full = [f for f, _ in experiments(RESULTS, "full")]
    breaker = [f for f, _ in experiments(RESULTS, "breaker")]
    if not full or not breaker:
        print("need the full experiment and the breaker experiment in results/")
        return 1
    out = RESULTS / "h3"
    judged, degraded = write_h3(full, breaker, out)
    print("\n".join(console(judged, degraded)))
    print(f"-> {out}/ h3.csv, h3.tex, opening.csv, degraded.tex, h3_c7.png, h3_opening.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
