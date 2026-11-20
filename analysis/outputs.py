"""Analysis outputs 2 and 3 of the midterm, and the A/A noise floor (M7).

Written next to the summary (analysis/summarize.py calls write_outputs):
  output2_rq1.tex    output 2: p50, p95, p99, error rate, reads measured against
                     predicted, throughput and dependency calls per request (a) for
                     C4 to C7, medians of the runs of each cell (RQ1, H1)
  output3_c7.png     output 3: error rate and p99 every 5 s through the C7 outage,
  output3_c7.pdf     one line per configuration (median over its runs)
  aa.csv, aa.tex     the A/A comparison: in each session, the regular Base C1 run
                     against the control run of the same cell, and the noise floor
  effects.csv        every comparison with Base, judged by both rules below
  output4_c3.png     output 4 (RQ2, H2): p95, hit ratio, cold starts, environments,
  output4_c3.pdf     concurrency and achieved rate per C3 step against arrival rate
  output4_c3.tex     the same medians as a table
  output5_cost.tex   output 5 (RQ3): cost per 1,000 successful requests per
                     configuration and condition, change against Base, stale rate

Rules (midterm Sections 4.6 and 5.2):
  - range rule: a configuration differs from Base only if their ranges of five runs
    do not overlap (verdict from analysis/summarize.py);
  - noise floor: two identical runs of the same session differ by some amount; an
    effect also has to be larger than the largest such A/A difference, or it is
    reported as below the noise floor.
"""
import csv
import statistics
from pathlib import Path

from analysis.plot_run import INK, MUTED, latency_axis, plt, read_csv, style
from orchestrate.plan import CONDITIONS, CONFIGURATIONS

AA_METRICS = ("error_rate", "p95_ms", "cost_per_1000_usd")     # the compared outcomes
COLOURS = {"Base": "#2a78d6", "Retry": "#eb6834", "Cache": "#1baf7a",
           "Retry+Cache": "#eda100"}


# ------------------------------------------------------------------ A/A noise floor

def aa_pairs(rows):
    """Per session: (experiment, regular Base C1 row, control row), where both exist."""
    pairs = []
    for experiment in sorted({r.get("experiment") for r in rows if r.get("control")}):
        same = [r for r in rows if r.get("experiment") == experiment
                and r["condition"] == "C1" and r["configuration"] == "Base"]
        regular = [r for r in same if not r.get("control")]
        control = [r for r in same if r.get("control")]
        if regular and control:
            pairs.append((experiment, regular[0], control[0]))
    return pairs


def noise_floor(pairs):
    """{metric: largest absolute A/A difference}; None for a metric no pair has."""
    floor = {}
    for metric in AA_METRICS:
        gaps = [abs(a[metric] - b[metric]) for _, a, b in pairs
                if a.get(metric) is not None and b.get(metric) is not None]
        floor[metric] = max(gaps) if gaps else None
    return floor


def judge_effects(cells, floor):
    """Every non-Base cell and compared metric: the range verdict, the difference of the
    medians from Base, and the final verdict once the noise floor is applied."""
    base = {c["condition"]: c for c in cells if c["configuration"] == "Base"}
    rows = []
    for cell in cells:
        if cell["configuration"] == "Base" or cell["condition"] not in base:
            continue
        for metric in AA_METRICS:
            verdict = cell.get(f"{metric}_vs_base", "")
            mine, theirs = cell[f"{metric}_median"], base[cell["condition"]][f"{metric}_median"]
            difference = None if mine is None or theirs is None else mine - theirs
            final = verdict
            if verdict in ("lower", "higher") and floor.get(metric) is not None \
                    and abs(difference) <= floor[metric]:
                final = "below noise floor"
            rows.append({"condition": cell["condition"], "configuration": cell["configuration"],
                         "metric": metric, "range_verdict": verdict,
                         "median_difference": difference, "noise_floor": floor.get(metric),
                         "final": final})
    return rows


def aa_tex(pairs, floor):
    lines = [r"\begin{tabular}{@{}lrrrrrr@{}}", r"\toprule",
             r"\textbf{Session} & \multicolumn{2}{c}{\textbf{Errors (\%)}} & "
             r"\multicolumn{2}{c}{\textbf{p95 (ms)}} & "
             r"\multicolumn{2}{c}{\textbf{\$/1{,}000 ($10^{-3}$)}} \\",
             r" & Base & A/A & Base & A/A & Base & A/A \\", r"\midrule"]
    for k, (_, a, b) in enumerate(pairs, 1):
        lines.append(f"{k} & {a['error_rate'] * 100:.2f} & {b['error_rate'] * 100:.2f} & "
                     f"{a['p95_ms']:.1f} & {b['p95_ms']:.1f} & "
                     f"{a['cost_per_1000_usd'] * 1000:.3f} & "
                     f"{b['cost_per_1000_usd'] * 1000:.3f} \\\\")
    if pairs:
        lines += [r"\midrule",
                  rf"Noise floor & \multicolumn{{2}}{{c}}{{{floor['error_rate'] * 100:.2f}}} & "
                  rf"\multicolumn{{2}}{{c}}{{{floor['p95_ms']:.1f}}} & "
                  rf"\multicolumn{{2}}{{c}}{{{floor['cost_per_1000_usd'] * 1000:.3f}}} \\"]
    lines += [r"\bottomrule", r"\end{tabular}", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ output 2

def pct(value, digits=2):
    return "--" if value is None else f"{value * 100:.{digits}f}"


def num(value, digits=1):
    return "--" if value is None else f"{value:,.{digits}f}"


def output2_tex(cells):
    """Output 2 (RQ1, H1): medians for C4 to C7; reads measured against predicted."""
    lines = [r"\begin{tabular}{@{}llrrrrrrrr@{}}", r"\toprule",
             r"\textbf{Cond.} & \textbf{Configuration} & \textbf{p50} & \textbf{p95} & "
             r"\textbf{p99} & \textbf{Errors (\%)} & \textbf{Reads (\%)} & "
             r"\textbf{Predicted (\%)} & \textbf{Req./s} & $\bar{a}$ \\", r"\midrule"]
    previous = None
    for cell in cells:
        if cell["condition"] not in ("C4", "C5", "C6", "C7"):
            continue
        if previous is not None and cell["condition"] != previous:
            lines.append(r"\midrule")
        previous = cell["condition"]
        lines.append(" & ".join([
            cell["condition"], cell["configuration"], num(cell["p50_ms_median"]),
            num(cell["p95_ms_median"]), num(cell["p99_ms_median"]),
            pct(cell["error_rate_median"]), pct(cell["read_error_rate_median"]),
            pct(cell.get("expected_read_error_rate")), num(cell["throughput_rps_median"]),
            num(cell["mean_attempts_median"], 2)]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ output 3

def c7_series(rows, experiment_dirs):
    """{configuration: [(bucket start s, median error rate, median p99 ms)]} over the
    C7 runs, from each run's buckets.csv (5 s buckets counted from RUN_START)."""
    folders = {Path(d).name: Path(d) for d in experiment_dirs}
    per_config = {}
    for r in rows:
        if r["condition"] != "C7" or r.get("control"):
            continue
        parent = folders.get(r.get("experiment"), Path(experiment_dirs[0])).parent
        buckets_path = parent / r["run_id"] / "buckets.csv"
        if buckets_path.exists():
            per_config.setdefault(r["configuration"], []).append(read_csv(buckets_path))
    series = {}
    for configuration, runs in per_config.items():
        starts = sorted({b["start_s"] for run in runs for b in run})
        points = []
        for start in starts:
            here = [b for run in runs for b in run if b["start_s"] == start and b["requests"]]
            if len(here) * 2 <= len(runs):          # an edge bucket that most runs do not
                continue                            # reach: no median to speak of
            points.append((start, statistics.median(b["error_rate"] for b in here),
                           statistics.median(b["p99_ms"] for b in here)))
        series[configuration] = points
    return series


def plot_output3(series, paths, runs_per_config=None):
    """Error rate (top) and p99 (bottom) through the C7 outage, shaded; one line each."""
    outage = (CONDITIONS["C7"]["FAULT_OUTAGE_START_S"], CONDITIONS["C7"]["FAULT_OUTAGE_END_S"])
    style()
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(10, 7), sharex=True,
                                      layout="constrained")
    for ax in (top, bottom):
        ax.axvspan(*outage, color="#e4e3df", alpha=0.7, linewidth=0)
    for configuration in CONFIGURATIONS:
        points = series.get(configuration)
        if not points:
            continue
        x = [p[0] + 2.5 for p in points]                    # middle of each bucket
        top.plot(x, [p[1] * 100 for p in points], color=COLOURS[configuration],
                 linewidth=1.8, label=configuration)
        bottom.plot(x, [p[2] for p in points], color=COLOURS[configuration], linewidth=1.8,
                    label=configuration)
    top.set_ylabel("Error rate (%)")
    top.set_ylim(0, 105)
    latency_axis(bottom, [p[2] for points in series.values() for p in points] or [100])
    bottom.annotate("outage (dependency down)", xy=(sum(outage) / 2, 7), ha="center",
                    va="center", fontsize=9, color=MUTED)
    handles, labels = top.get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside upper center", ncol=4, frameon=False)
    bottom.set_ylabel("p99 latency (ms)")
    bottom.set_xlabel("Seconds from the planned start of the measured phase")
    runs = f" (median of {runs_per_config} runs per configuration)" if runs_per_config else ""
    top.set_title(f"Output 3: C7, 1-minute outage, every 5 s{runs}", color=INK)
    for path in paths:
        fig.savefig(path, dpi=200)
    plt.close(fig)


# ------------------------------------------------------------------ output 4

STEP_METRICS = (("p95_ms", "p95 latency (ms)"), ("hit_ratio", "Cache hit ratio (%)"),
                ("cold_starts", "Cold starts per step"),
                ("environments", "Execution environments seen"),
                ("concurrency", "Concurrent executions (estimate)"),
                ("requests", "Achieved rate (requests/s)"))


def c3_steps_by_config(rows, experiment_dirs):
    """{configuration: [per-step medians over its C3 runs]} from each run's steps.csv."""
    folders = {Path(d).name: Path(d) for d in experiment_dirs}
    per_config = {}
    for r in rows:
        if r["condition"] != "C3" or r.get("control"):
            continue
        parent = folders.get(r.get("experiment"), Path(experiment_dirs[0])).parent
        path = parent / r["run_id"] / "steps.csv"
        if path.exists():
            per_config.setdefault(r["configuration"], []).append(read_csv(path))
    out = {}
    for configuration, runs in per_config.items():
        steps = []
        for k in sorted({s["step"] for run in runs for s in run}):
            here = [s for run in runs for s in run if s["step"] == k]
            step = {"step": k, "target_rate": here[0]["target_rate"], "runs": len(here)}
            for metric, _ in STEP_METRICS + (("error_rate", ""),):
                values = [s[metric] for s in here if s.get(metric) is not None]
                step[metric] = statistics.median(values) if values else None
            steps.append(step)
        out[configuration] = steps
    return out


def plot_output4(steps, paths):
    """Six panels against arrival rate (log scale), one line per configuration."""
    style()
    fig, axes = plt.subplots(2, 3, figsize=(12, 7), layout="constrained")
    rates = sorted({s["target_rate"] for v in steps.values() for s in v})
    for ax, (metric, label) in zip(axes.flat, STEP_METRICS):
        for configuration in CONFIGURATIONS:
            points = [s for s in steps.get(configuration, []) if s.get(metric) is not None]
            if not points:
                continue
            scale = 100 if metric == "hit_ratio" else 1 / 60 if metric == "requests" else 1
            ax.plot([s["target_rate"] for s in points], [s[metric] * scale for s in points],
                    color=COLOURS[configuration], linewidth=1.8, marker="o", markersize=4,
                    label=configuration)
        ax.set_xscale("log")
        ax.set_xticks(rates, [f"{r:,}" for r in rates])
        ax.minorticks_off()
        ax.set_title(label, fontsize=9, loc="left")
        ax.set_xlabel("Arrival rate (requests/s)")
        if metric != "p95_ms":
            ax.set_ylim(bottom=0)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside upper center", ncol=4, frameon=False)
    for path in paths:
        fig.savefig(path, dpi=200)
    plt.close(fig)


def output4_tex(steps):
    lines = [r"\begin{tabular}{@{}lrrrrrr@{}}", r"\toprule",
             r"\textbf{Configuration} & \textbf{Rate} & \textbf{p95 (ms)} & $H$ (\%) & "
             r"\textbf{Cold starts} & \textbf{Environments} & \textbf{Concurrency} \\",
             r"\midrule"]
    first = True
    for configuration in CONFIGURATIONS:
        if configuration not in steps:
            continue
        if not first:
            lines.append(r"\midrule")
        first = False
        for s in steps[configuration]:
            lines.append(" & ".join([configuration, f"{s['target_rate']:,}", num(s["p95_ms"]),
                                     pct(s["hit_ratio"], 1), num(s["cold_starts"], 0),
                                     num(s["environments"], 0), num(s["concurrency"], 1)])
                         + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ output 5

def output5_tex(cells):
    """Cost per 1,000 successful requests (thousandths of a dollar) and change against
    Base, per configuration and condition; then the stale rate of the cache rows."""
    grid = {(c["configuration"], c["condition"]): c for c in cells}
    conditions = [k for k in CONDITIONS if any(c["condition"] == k for c in cells)]
    lines = [r"\begin{tabular}{@{}l" + "r" * len(conditions) + r"@{}}", r"\toprule",
             r"\textbf{Configuration} & " + " & ".join(rf"\textbf{{{k}}}" for k in conditions)
             + r" \\", r"\midrule",
             rf"\multicolumn{{{len(conditions) + 1}}}{{@{{}}l}}{{\emph{{Cost per 1{{,}}000 "
             r"successful requests (thousandths of a US dollar; change against Base)}} \\"]
    for configuration in CONFIGURATIONS:
        cells_ = [grid.get((configuration, k)) for k in conditions]
        if not any(cells_):
            continue
        texts = []
        for k, cell in zip(conditions, cells_):
            value = None if cell is None else cell["cost_per_1000_usd_median"]
            base = grid.get(("Base", k))
            base_value = None if base is None else base["cost_per_1000_usd_median"]
            if value is None:
                texts.append("--")
            elif configuration == "Base" or not base_value:
                texts.append(f"{value * 1000:.2f}")
            else:
                texts.append(f"{value * 1000:.2f} ({(value / base_value - 1) * 100:+.0f}\\%)")
        lines.append(configuration + " & " + " & ".join(texts) + r" \\")
    lines += [r"\midrule",
              rf"\multicolumn{{{len(conditions) + 1}}}{{@{{}}l}}{{\emph{{Stale responses "
              r"(\% of cache hits)}} \\"]
    for configuration in CONFIGURATIONS:
        if "Cache" not in configuration:
            continue
        lines.append(configuration + " & " + " & ".join(
            pct((grid.get((configuration, k)) or {}).get("stale_rate_median"), 1)
            for k in conditions) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ all together

def write_outputs(rows, experiment_dirs, cells, out_dir):
    """Write outputs 2 and 3, the A/A table and the judged effects into out_dir."""
    out_dir = Path(out_dir)
    (out_dir / "output2_rq1.tex").write_text(output2_tex(cells))
    series = c7_series(rows, list(experiment_dirs))
    if series:
        runs = max(len([r for r in rows if r["condition"] == "C7" and not r.get("control")
                        and r["configuration"] == k]) for k in series)
        plot_output3(series, [out_dir / "output3_c7.png", out_dir / "output3_c7.pdf"], runs)
    pairs = aa_pairs(rows)
    floor = noise_floor(pairs)
    with open(out_dir / "aa.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["experiment"] + [f"{m}_{w}" for m in AA_METRICS
                                          for w in ("base", "control")])
        for experiment, a, b in pairs:
            writer.writerow([experiment] + [x for m in AA_METRICS for x in (a[m], b[m])])
        writer.writerow(["noise_floor"] + [x for m in AA_METRICS for x in (floor[m], "")])
    (out_dir / "aa.tex").write_text(aa_tex(pairs, floor))
    steps = c3_steps_by_config(rows, list(experiment_dirs))
    if steps:
        plot_output4(steps, [out_dir / "output4_c3.png", out_dir / "output4_c3.pdf"])
        (out_dir / "output4_c3.tex").write_text(output4_tex(steps))
    (out_dir / "output5_cost.tex").write_text(output5_tex(cells))
    effects = judge_effects(cells, floor)
    if effects:
        with open(out_dir / "effects.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(effects[0]))
            writer.writeheader()
            writer.writerows(effects)
    return {"pairs": len(pairs), "floor": floor, "effects": effects, "c7": bool(series),
            "c3": bool(steps)}
