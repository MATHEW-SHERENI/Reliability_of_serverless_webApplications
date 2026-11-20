"""One figure with every metric of one run (M7), from the files analysis/metrics.py wrote.

Six panels:
  (a) latency over time: p50, p95, p99 per 5 s bucket, with the 500 ms SLO line
  (b) throughput: successful and failed responses per second, per 5 s bucket
  (c) SLO verdict per 1-minute window: p95 and error rate, pass or fail
  (d) failures by cause: timeout, throttled, dependency, catalog, platform
  (e) every other metric of the run, as a table
  (f) cost per 1,000 successful requests, split into its billed parts
Time in (a) and (b) counts from the planned start of the measured phase
(RUN_START); a C7 outage is shaded there.

Usage: .venv/bin/python analysis/plot_run.py results/<run_id>
Writes results/<run_id>/figure.png (for viewing) and figure.pdf (for the report).
Run analysis/metrics.py on the run first.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")                    # draw to files; no window needed
import matplotlib.pyplot as plt          # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator, NullFormatter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.metrics import SLO_ERROR_RATE, SLO_P95_MS, cost_parts  # noqa: E402

# Colours (checked for colour-blind separation): one blue, light to dark, for the
# three percentiles; blue and red for success and failure; green and red for pass
# and fail, always with the words PASS or FAIL beside them.
P50, P95, P99 = "#6da7ec", "#256abf", "#0d366b"
SUCCESS, FAILURE = "#2a78d6", "#d03b3b"
PASS, FAIL = "#0ca30c", "#d03b3b"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
CONDITION_NAMES = {"C1": "healthy", "C2": "cold-start burst", "C3": "load ramp",
                   "C4": "10% transient faults", "C5": "+300 ms delay",
                   "C6": "+1.5 s delay", "C7": "1-minute outage"}
LATENCY_TICKS = [10, 30, 100, 300, 1000, 3000, 10000]


def read_csv(path):
    with open(path, newline="") as f:
        return [{k: number(v) for k, v in row.items()} for row in csv.DictReader(f)]


def number(text):
    """CSV text back to a value: '' -> None, 'True' -> True, '3' -> 3, '0.5' -> 0.5."""
    if text in ("", "None"):
        return None
    if text in ("True", "False"):
        return text == "True"
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return text


def style():
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Liberation Serif", "Nimbus Roman", "DejaVu Serif"],
        "mathtext.fontset": "stix",          # serif maths, matching the text
        "font.size": 9, "axes.titlesize": 10, "axes.titleweight": "bold",
        "axes.titlelocation": "left", "axes.edgecolor": MUTED, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.axisbelow": True, "legend.frameon": False, "legend.fontsize": 8,
    })


def latency_axis(ax, values):
    """Log scale: 30 ms and 10 s on one readable axis, the same for every run."""
    ax.set_yscale("log")
    top = max([v for v in values if v] + [SLO_P95_MS])
    ax.set_ylim(5, top * 2)
    ax.set_yticks([t for t in LATENCY_TICKS if t <= top * 2])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_ylabel("Latency (ms)")


def slo_line(ax):
    ax.axhline(SLO_P95_MS, color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
    ax.annotate("SLO p95 500 ms", xy=(1, SLO_P95_MS), xycoords=("axes fraction", "data"),
                xytext=(0, 3), textcoords="offset points", ha="right", va="bottom",
                fontsize=8, color=MUTED)


def shade_outage(ax, faults):
    if faults.get("FAULT_MODE") == "outage":
        ax.axvspan(int(faults["FAULT_OUTAGE_START_S"]), int(faults["FAULT_OUTAGE_END_S"]),
                   color=GRID, alpha=0.7, linewidth=0, label="Outage")


def panel_latency(ax, buckets, faults):
    shade_outage(ax, faults)
    x = [b["start_s"] + 2.5 for b in buckets]                  # middle of each bucket
    for key, colour, name in (("p99_ms", P99, "p99"), ("p95_ms", P95, "p95"),
                              ("p50_ms", P50, "p50")):
        ax.plot(x, [b[key] for b in buckets], color=colour, linewidth=1.6, label=name)
    latency_axis(ax, [b["p99_ms"] for b in buckets])
    slo_line(ax)
    ax.set_xlabel("Seconds from planned start of the measured phase")
    ax.set_title("(a) Latency per 5 s")
    ax.legend(loc="upper left", ncol=4)


def panel_throughput(ax, buckets, faults):
    shade_outage(ax, faults)
    x = [b["start_s"] + 2.5 for b in buckets]
    ok = [(b["requests"] - b["failed"]) / 5 for b in buckets]
    bad = [b["failed"] / 5 for b in buckets]
    ax.bar(x, ok, width=4.4, color=SUCCESS, label="Successful")
    ax.bar(x, bad, width=4.4, bottom=ok, color=FAILURE, label="Failed")
    ax.set_ylim(0, max(o + f for o, f in zip(ok, bad)) * 1.25)
    ax.set_ylabel("Responses per second")
    ax.set_xlabel("Seconds from planned start of the measured phase")
    ax.set_title("(b) Throughput per 5 s")
    ax.legend(loc="upper left", ncol=3)


def panel_windows(ax, windows):
    x = list(range(1, len(windows) + 1))
    colours = [PASS if w["meets_slo"] else FAIL for w in windows]
    p95 = [w["p95_ms"] or 0 for w in windows]
    ax.bar(x, p95, width=0.55, color=colours)
    latency_axis(ax, p95)
    slo_line(ax)
    for xi, w in zip(x, windows):
        verdict = "PASS" if w["meets_slo"] else "FAIL"
        errors = "no requests" if not w["requests"] else f"{w['error_rate']:.2%} errors"
        height = w["p95_ms"] or 5
        ax.annotate(f"{verdict}\np95 {height:,.0f} ms\n{errors}", xy=(xi, height),
                    xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                    fontsize=8)
    ax.set_xticks(x, [f"Minute {i}" for i in x])
    ax.grid(axis="x", visible=False)
    ax.set_ylim(top=ax.get_ylim()[1] * 4)          # room for the labels
    ax.set_title(f"(c) SLO per minute: p95 ≤ {SLO_P95_MS} ms and errors "
                 f"≤ {SLO_ERROR_RATE:.0%}")


def panel_failures(ax, row):
    kinds = [("timeout", "No answer\n(10 s timeout)"), ("throttled", "Throttled\n(429)"),
             ("dependency", "Dependency\nfailed (5xx)"), ("catalog", "Catalog\nbug (5xx)"),
             ("platform", "Gateway or\nLambda (5xx)")]
    counts = [row[f"fail_{k}"] for k, _ in kinds]
    y = list(range(len(kinds)))[::-1]
    ax.barh(y, counts, height=0.55, color=FAILURE)
    ax.set_yticks(y, [label for _, label in kinds])
    ax.set_xlim(0, max(counts + [1]) * 1.3)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))     # counts are whole numbers
    if not any(counts):
        ax.text(0.5, 0.5, "No failed requests", transform=ax.transAxes, ha="center",
                va="center", fontsize=10, color=MUTED)
    for yi, c in zip(y, counts):
        ax.annotate(f"{c:,}", xy=(c, yi), xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=8)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Failed requests")
    ax.set_title(f"(d) Failures by cause: {row['failed']:,} of {row['requests']:,} "
                 f"({row['error_rate']:.2%})")


def fmt(value, kind="n"):
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value:.2%}"
    if kind == "ms":
        return f"{value:,.1f} ms"
    if kind == "s":
        return f"{value:,.0f} s"
    if isinstance(value, float):
        return f"{value:,.3g}" if abs(value) < 1000 else f"{value:,.0f}"
    return f"{value:,}"


def panel_table(ax, row):
    rows = [
        ("Requests (measured phase)", fmt(row["requests"])),
        ("Successful / failed", f"{row['successes']:,} / {row['failed']:,}"),
        ("Error rate: all / reads / orders",
         f"{fmt(row['error_rate'], 'pct')} / {fmt(row.get('read_error_rate'), 'pct')} / "
         f"{fmt(row.get('order_error_rate'), 'pct')}"),
        ("p50 / p95 / p99", f"{row['p50_ms']:,.1f} / {row['p95_ms']:,.1f} / "
                            f"{row['p99_ms']:,.1f} ms"),
        ("Throughput", f"{row['throughput_rps']:,.1f} per s over {fmt(row['duration_s'], 's')}"),
        ("Dependency calls per request (ā)", fmt(row["mean_attempts"])),
        ("Cache hit ratio (H)", fmt(row["hit_ratio"], "pct")),
        ("Stale reads", "n/a" if row["stale_rate"] is None else
         f"{row['stale_reads']:,} ({row['stale_rate']:.2%} of hits)"),
        ("Cold starts: Catalog / dependency",
         f"{row['cold_starts']:,} / {row['dependency_cold_starts']:,}"),
        ("SLO-compliant minutes", f"{row['slo_compliant_windows']} of {row['slo_windows']}"),
        ("Recovery time R (C7 only)", fmt(row["recovery_s"], "s")),
        ("Requests matched to Catalog logs", fmt(row["joined_share"], "pct")),
        ("Invocations: Catalog / dependency",
         f"{row['catalog_invocations']:,} / {row['dependency_invocations']:,}"),
        ("Injected faults", fmt(row["injected_faults"])),
        ("Run cost (measured phase)", f"${row['cost_usd']:.5f}"),
    ]
    ax.axis("off")
    ax.set_title("(e) All metrics of the run")
    table = ax.table(cellText=rows, colLabels=["Metric", "Value"], colLoc="left",
                     cellLoc="left", loc="upper left", colWidths=[0.6, 0.4],
                     bbox=[0, 0, 1, 0.97])
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    for (r, _), cell in table.get_celld().items():
        cell.set_edgecolor(GRID)
        cell.set_linewidth(0.6)
        if r == 0:
            cell.set_text_props(fontweight="bold")


def panel_cost(ax, row):
    parts = cost_parts(row["gateway_requests"],
                       row["catalog_invocations"] + row["dependency_invocations"],
                       row["gb_seconds"], row["read_units"], row["write_units"])
    total = sum(parts.values())
    if row["successes"]:
        shown = {name: 1000 * dollars / row["successes"] for name, dollars in parts.items()}
        unit = "US dollars per 1,000 successful requests"
        title = f"(f) Cost: ${row['cost_per_1000_usd']:.5f} per 1,000 successful requests"
    else:
        # Nothing succeeded (C6): a cost per success does not exist, so show what the
        # run itself cost; the money was spent all the same.
        shown = dict(parts)
        unit = "US dollars for the whole measured phase"
        title = (f"(f) Cost: ${total:.5f} for the run; no successful requests, "
                 "so no cost per success")
    names = sorted(shown, key=shown.get)                          # largest at the top
    values = [shown[n] for n in names]
    ax.barh(names, values, height=0.55, color=SUCCESS)
    ax.set_xlim(0, (max(values) or 1e-6) * 1.6)
    for name, v in zip(names, values):
        share = f"{parts[name] / total:.0%}" if total else "n/a"
        ax.annotate(f"${v:.5f}  ({share})", xy=(v, name), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=8)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:.4f}"))
    ax.grid(axis="y", visible=False)
    ax.set_xlabel(unit)
    ax.set_title(title)


def plot_run(run_dir):
    """Draw the figure of one run; returns the paths written."""
    run_dir = Path(run_dir)
    if not (run_dir / "metrics.csv").exists():
        raise FileNotFoundError(f"{run_dir}/metrics.csv missing: run analysis/metrics.py first")
    row = read_csv(run_dir / "metrics.csv")[0]
    buckets = read_csv(run_dir / "buckets.csv")
    windows = read_csv(run_dir / "windows.csv")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    faults = manifest.get("faults") or {}

    style()
    fig, axes = plt.subplots(3, 2, figsize=(11, 12.5), layout="constrained")
    panel_latency(axes[0, 0], buckets, faults)
    panel_throughput(axes[0, 1], buckets, faults)
    panel_windows(axes[1, 0], windows)
    panel_failures(axes[1, 1], row)
    panel_table(axes[2, 0], row)
    panel_cost(axes[2, 1], row)
    condition = row["condition"]
    fig.suptitle(f"{row['configuration']} under {condition} "
                 f"({CONDITION_NAMES.get(condition, condition)}), repetition "
                 f"{row['repetition']}\n{row['run_id']}", x=0.01, ha="left",
                 fontsize=12, fontweight="bold")
    paths = [run_dir / "figure.png", run_dir / "figure.pdf"]
    fig.savefig(paths[0], dpi=200)
    fig.savefig(paths[1])
    plt.close(fig)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", help="results/<run_id>, after analysis/metrics.py")
    args = parser.parse_args()
    for path in plot_run(args.run):
        print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
