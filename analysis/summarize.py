"""Experiment summary (M7): one row per cell with the median and range of its runs.

Input: results/<experiment_id>/metrics.csv, one row per valid run (written by
analysis/metrics.py). Outputs in the same folder:
  summary.csv      one row per cell (condition x configuration): runs, and the
                   median, minimum and maximum of every metric
  summary.tex      the main values as a LaTeX table for the report
  comparison.png   the four configurations side by side, one row per condition
  comparison.pdf   the same figure for the report

Rules (midterm Section 4.6):
  - Each cell is described by the median and the range (minimum to maximum) of
    its runs, five in the full experiment.
  - A configuration differs from Base only if their ranges do not overlap; with
    fewer than five runs in either cell no verdict is given.
  - Under C4 the measured error rates are set against the analytical ones: a
    read fails only if every attempt fails (p for Base, p^4 with four attempts),
    and an order fails at p in every configuration, because orders are never
    retried or cached.

Usage: .venv/bin/python analysis/summarize.py results/<experiment_id>
"""
import argparse
import csv
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.plot_run import INK, MUTED, latency_axis, plt, read_csv, style  # noqa: E402
from orchestrate.plan import CONDITIONS, CONFIGURATIONS  # noqa: E402

METRICS = ("error_rate", "read_error_rate", "order_error_rate", "p50_ms", "p95_ms", "p99_ms",
           "throughput_rps", "mean_attempts", "hit_ratio", "stale_rate", "cold_starts",
           "slo_compliant_share", "recovery_s", "cost_per_1000_usd")
COMPARED = ("error_rate", "p95_ms", "cost_per_1000_usd")   # the outcomes of RQ1 to RQ3
MIN_RUNS = 5               # repetitions per cell in the full experiment
MAX_ATTEMPTS = 4           # as MAX_ATTEMPTS in api/reliability.py
FAULT_RATE = CONDITIONS["C4"]["FAULT_RATE"]
COLOURS = {"Base": "#2a78d6", "Retry": "#eb6834", "Cache": "#1baf7a",
           "Retry+Cache": "#eda100"}
CONDITION_NAMES = {"C1": "C1 healthy", "C2": "C2 cold-start burst", "C3": "C3 load ramp",
                   "C4": "C4 10% transient faults", "C5": "C5 +300 ms delay",
                   "C6": "C6 +1.5 s delay", "C7": "C7 1-minute outage"}


# ------------------------------------------------------------------ the numbers

def median_range(values):
    """(median, minimum, maximum) of the values that are not None; all None if none are."""
    present = [v for v in values if v is not None]
    if not present:
        return None, None, None
    return statistics.median(present), min(present), max(present)


def ranges_overlap(a, b):
    """True if the ranges a = (min, max) and b = (min, max) share a point; touching counts."""
    return a[0] <= b[1] and b[0] <= a[1]


def compare(cell, base, metric):
    """How a cell's metric stands against Base: lower, higher, inconclusive or too few runs."""
    if cell["runs"] < MIN_RUNS or base["runs"] < MIN_RUNS:
        return "too few runs"
    mine = (cell[f"{metric}_min"], cell[f"{metric}_max"])
    theirs = (base[f"{metric}_min"], base[f"{metric}_max"])
    if None in mine or None in theirs:
        return "too few runs"
    if ranges_overlap(mine, theirs):
        return "inconclusive"
    return "lower" if mine[1] < theirs[0] else "higher"


def expected_rates(condition, configuration):
    """(read, order) error rates predicted for C4; (None, None) where there is no prediction.

    With the cache on, a read's failure chance depends on the measured hit ratio,
    so only Base and Retry get a read prediction. Orders bypass both mechanisms.
    """
    if condition != "C4":
        return None, None
    read = {"Base": FAULT_RATE, "Retry": FAULT_RATE ** MAX_ATTEMPTS}.get(configuration)
    return read, FAULT_RATE


def summarise(rows):
    """One summary row per cell, conditions C1..C7 and configurations in plan order."""
    cells = []
    for condition in CONDITIONS:
        for configuration in CONFIGURATIONS:
            runs = [r for r in rows if r["condition"] == condition
                    and r["configuration"] == configuration]
            if not runs:
                continue
            cell = {"condition": condition, "configuration": configuration, "runs": len(runs)}
            for metric in METRICS:
                stats = median_range([r.get(metric) for r in runs])
                cell.update(zip((f"{metric}_median", f"{metric}_min", f"{metric}_max"),
                                stats))
            read, order = expected_rates(condition, configuration)
            cell["expected_read_error_rate"] = read
            cell["expected_order_error_rate"] = order
            cells.append(cell)
    for cell in cells:
        base = next((c for c in cells if c["condition"] == cell["condition"]
                     and c["configuration"] == "Base"), None)
        for metric in COMPARED:
            verdict = ""
            if base is not None and cell["configuration"] != "Base":
                verdict = compare(cell, base, metric)
            cell[f"{metric}_vs_base"] = verdict
    return cells


# ------------------------------------------------------------------ LaTeX table

def cell_text(cell, metric, scale=1.0, digits=2):
    """'median (min--max)' when the cell has several runs, else the median; '--' if none."""
    median = cell[f"{metric}_median"]
    if median is None:
        return "--"
    text = f"{median * scale:.{digits}f}"
    if cell["runs"] > 1:
        text += (f" ({cell[f'{metric}_min'] * scale:.{digits}f}--"
                 f"{cell[f'{metric}_max'] * scale:.{digits}f})")
    return text


def latex_table(cells):
    """A booktabs tabular (no preamble); the report adds the caption and label."""
    lines = [
        r"\begin{tabular}{@{}llrrrrrrrr@{}}",
        r"\toprule",
        r"\textbf{Cond.} & \textbf{Configuration} & \textbf{Runs} & \textbf{Errors (\%)} & "
        r"\textbf{Reads (\%)} & \textbf{Orders (\%)} & \textbf{p95 (ms)} & $\bar{a}$ & "
        r"$H$ (\%) & \textbf{\$/1{,}000 ($10^{-3}$)} \\",
        r"\midrule",
    ]
    previous = None
    for cell in cells:
        if previous is not None and cell["condition"] != previous:
            lines.append(r"\midrule")
        previous = cell["condition"]
        lines.append(" & ".join([
            cell["condition"], cell["configuration"].replace("&", r"\&"), str(cell["runs"]),
            cell_text(cell, "error_rate", 100), cell_text(cell, "read_error_rate", 100),
            cell_text(cell, "order_error_rate", 100), cell_text(cell, "p95_ms", digits=1),
            cell_text(cell, "mean_attempts"), cell_text(cell, "hit_ratio", 100, 1),
            cell_text(cell, "cost_per_1000_usd", 1000)]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ figure

PANELS = (("error_rate", "Error rate (%)", 100, "{:.2f}"),
          ("p95_ms", "p95 latency (ms)", 1, "{:,.0f}"),
          ("cost_per_1000_usd", r"Cost per 1,000 successful ($10^{-3}$ US\$)", 1000, "{:.2f}"),
          ("hit_ratio", "Cache hit ratio (%)", 100, "{:.0f}"))


def panel(ax, cells, metric, label, scale, form):
    names = [c["configuration"] for c in cells]
    x = range(len(cells))
    medians = [(c[f"{metric}_median"] or 0) * scale for c in cells]
    ax.bar(x, medians, width=0.62, color=[COLOURS[n] for n in names])
    for xi, c in zip(x, cells):
        median = c[f"{metric}_median"]
        if median is not None and c["runs"] > 1:
            ax.plot([xi, xi], [c[f"{metric}_min"] * scale, c[f"{metric}_max"] * scale],
                    color=INK, linewidth=1)
        top = (c[f"{metric}_max"] if c["runs"] > 1 and median is not None else median)
        text = "n/a" if median is None else form.format(median * scale)
        ax.annotate(text, xy=(xi, (top or 0) * scale), xytext=(0, 3),
                    textcoords="offset points", ha="center", va="bottom", fontsize=8,
                    color=MUTED if median is None else INK)
    if metric == "p95_ms":
        latency_axis(ax, [(c["p95_ms_max"] or 0) for c in cells])
        ax.set_ylabel("")                       # the panel title already names the unit
    else:
        ax.set_ylim(0, max(max(medians + [0]) * 1.3,
                           max([(c[f"{metric}_max"] or 0) * scale * 1.15 for c in cells])) or 1)
    ax.set_xticks(list(x), [n.replace("+", "+\n") for n in names])
    ax.grid(axis="x", visible=False)
    ax.set_title(label, fontsize=9, loc="left")


def plot_comparison(cells, paths):
    """One row per condition, four panels per row; bars are medians, whiskers the range."""
    conditions = [c for c in CONDITIONS if any(cell["condition"] == c for cell in cells)]
    style()
    fig, axes = plt.subplots(len(conditions), len(PANELS), squeeze=False,
                             figsize=(12, 3.3 * len(conditions)), layout="constrained")
    for row, condition in zip(axes, conditions):
        mine = [c for c in cells if c["condition"] == condition]
        for ax, (metric, label, scale, form) in zip(row, PANELS):
            panel(ax, mine, metric, label, scale, form)
        runs = sorted({c["runs"] for c in mine})
        row[0].annotate(f"{CONDITION_NAMES.get(condition, condition)}  "
                        f"(runs per cell: {', '.join(map(str, runs))})",
                        xy=(0, 1.16), xycoords="axes fraction", fontsize=11,
                        fontweight="bold")
    for path in paths:
        fig.savefig(path, dpi=200)
    plt.close(fig)


# ------------------------------------------------------------------ files

def summarise_experiment(experiment_dir):
    """Read metrics.csv, write the summary, the table and the figure; return the cells."""
    experiment_dir = Path(experiment_dir)
    source = experiment_dir / "metrics.csv"
    if not source.exists():
        raise FileNotFoundError(f"{source} missing: run analysis/metrics.py on the experiment")
    cells = summarise(read_csv(source))
    if not cells:
        raise ValueError(f"{source} has no runs")
    with open(experiment_dir / "summary.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(cells[0]))
        writer.writeheader()
        writer.writerows(cells)
    (experiment_dir / "summary.tex").write_text(latex_table(cells))
    plot_comparison(cells, [experiment_dir / "comparison.png", experiment_dir / "comparison.pdf"])
    return cells


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("experiment", help="results/<experiment_id>, after analysis/metrics.py")
    args = parser.parse_args()
    folder = Path(args.experiment)
    cells = summarise_experiment(folder)
    print(f"{'cell':22} {'runs':>4} {'errors':>8} {'reads':>8} {'orders':>8} "
          f"{'p95 ms':>7} {'$/1000':>8}")
    for c in cells:
        def pct(key):
            value = c[f"{key}_median"]
            return "n/a" if value is None else f"{value:.2%}"
        print(f"{c['condition'] + ' ' + c['configuration']:22} {c['runs']:>4} "
              f"{pct('error_rate'):>8} {pct('read_error_rate'):>8} {pct('order_error_rate'):>8} "
              f"{c['p95_ms_median']:>7.1f} {c['cost_per_1000_usd_median']:>8.5f}")
    for name in ("summary.csv", "summary.tex", "comparison.png", "comparison.pdf"):
        print(f"-> {folder / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
