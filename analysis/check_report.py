"""Does the report still match the data? (make check-report)

Run after the results have been regenerated from the raw run files (make analyze,
make results PLAN=experiments/breaker.json, make h3). Every table of the report
that comes from the experiment is checked against those files:

  * tables the analysis writes as LaTeX (output 1, the A/A comparison, outputs 2,
    4 and 5, the H3 comparison, breakers per environment): every data row must
    appear in report/final_report.tex as it is, apart from spacing;
  * the effects table and the read-p95 table, which the report lays out by hand:
    every number must equal the regenerated value, rounded to the decimals that
    the report shows.

Prints one line per table and exits with 1 if anything differs, so a changed
number can never slip into the report unnoticed.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.plot_run import read_csv  # noqa: E402

REPORT = ROOT / "report" / "final_report.tex"
FULL = ROOT / "results" / "full-summary"
H3 = ROOT / "results" / "h3"
GENERATED = (("output 1, SLO verdict matrix", FULL / "slo_matrix.tex"),
             ("A/A noise floor", FULL / "aa.tex"),
             ("output 2, RQ1", FULL / "output2_rq1.tex"),
             ("output 4, C3 steps", FULL / "output4_c3.tex"),
             ("output 5, cost and staleness", FULL / "output5_cost.tex"),
             ("H3, with and without the breaker", H3 / "h3.tex"),
             ("H3, breakers per environment", H3 / "degraded.tex"))
CONDITIONS = ("C1", "C2", "C3", "C4", "C5", "C6", "C7")


def squeeze(line):
    """One line with its runs of spaces made single, for comparing rows."""
    return " ".join(line.split())


def data_rows(tex):
    """The rows of a tabular that carry data. Header rows (bold) and unit rows (which
    start with &) are left out; section labels have no & and drop out by themselves."""
    rows = []
    for line in tex.splitlines():
        line = squeeze(line)
        if "&" in line and line.endswith("\\\\") and r"\textbf" not in line \
                and not line.startswith("&"):
            rows.append(line)
    return rows


def missing_rows(generated_tex, report_tex):
    """(number of data rows, the rows that do not appear in the report)."""
    present = {squeeze(line) for line in report_tex.splitlines()}
    rows = data_rows(generated_tex)
    return len(rows), [r for r in rows if r not in present]


# ------------------------------------------------------------------ tables laid out by hand

def table_block(report_tex, label):
    """The text of the table environment that carries this label."""
    end = report_tex.index(rf"\label{{{label}}}")
    start = report_tex.rindex(r"\begin{table}", 0, end)
    return report_tex[start:end]


def shown(cell):
    """The numbers a cell shows, as strings, in order: "9.98 (9.65--10.37)" gives
    9.98, 9.65, 10.37; "--" gives None; thousands commas are dropped, and arrows
    hold no digits, so they drop out by themselves."""
    if cell.strip() == "--":
        return [None]
    return [n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", cell)]


def agrees(text, value):
    """Whether the report's text is the value rounded to the decimals it shows."""
    if text is None or value is None:
        return text is None and value is None
    decimals = len(text.split(".")[1]) if "." in text else 0
    return abs(float(text) - value) <= 0.5 * 10 ** -decimals + 1e-9


def scaled(cell, metric, scale, parts=("median", "min", "max")):
    values = [cell.get(f"{metric}_{p}") for p in parts]
    return [None if v is None else v * scale for v in values]


def effects_expected(cell):
    """Columns of the effects table: errors, p95 and cost as median (min--max),
    then the median hit ratio and stale share."""
    return [scaled(cell, "error_rate", 100), scaled(cell, "p95_ms", 1),
            scaled(cell, "cost_per_1000_usd", 1000), scaled(cell, "hit_ratio", 100, ("median",)),
            scaled(cell, "stale_rate", 100, ("median",))]


def compare_columns(where, cells_shown, columns):
    """Mismatches between one row's shown cells and its expected columns."""
    problems = []
    for k, (texts, values) in enumerate(zip(cells_shown, columns)):
        if texts == [None]:
            values = values[:1]              # "--" stands for an undefined median
        if len(texts) != len(values) or not all(map(agrees, texts, values)):
            problems.append(f"{where}, column {k + 1}: report {texts}, data "
                            f"{[None if v is None else round(v, 3) for v in values]}")
    return problems


def check_effects(report_tex, cells):
    """(numbers checked, problems) for the effects table."""
    by = {(c["condition"], c["configuration"]): c for c in cells}
    checked, problems = 0, []
    for line in data_rows(table_block(report_tex, "tab:effects")):
        parts = [p.strip() for p in line.rstrip("\\").split("&")]
        cell = by.get((parts[0], parts[1]))
        if cell is None:
            problems.append(f"{parts[0]} {parts[1]}: no such cell in the data")
            continue
        cells_shown = [shown(p) for p in parts[2:]]
        checked += sum(len(t) for t in cells_shown)
        problems += compare_columns(f"{parts[0]} {parts[1]}", cells_shown, effects_expected(cell))
    return checked, problems


def check_read_p95(report_tex, cells):
    """(numbers checked, problems) for the read-p95 table: one median per condition."""
    by = {(c["condition"], c["configuration"]): c for c in cells}
    checked, problems = 0, []
    for line in data_rows(table_block(report_tex, "tab:readp95")):
        parts = [p.strip() for p in line.rstrip("\\").split("&")]
        name, cells_shown = parts[0], [shown(p) for p in parts[1:]]
        columns = [[(by.get((cond, name)) or {}).get("read_p95_ms_median")] for cond in CONDITIONS]
        checked += len(cells_shown)
        problems += compare_columns(name, cells_shown, columns)
    return checked, problems


# ------------------------------------------------------------------ all together

def check(report_tex, cells, generated):
    """One (name, ok, message, problems) per table; generated is (name, tex or None)."""
    results = []
    for name, tex in generated:
        if tex is None:
            results.append((name, False, "not regenerated: file missing", []))
            continue
        total, missing = missing_rows(tex, report_tex)
        results.append((name, not missing and total > 0,
                        f"{total - len(missing)} of {total} rows in the report", missing))
    for name, function in (("effects table", check_effects), ("read p95 table", check_read_p95)):
        checked, problems = function(report_tex, cells)
        results.append((name, not problems and checked > 0,
                        f"{checked} numbers, {len(problems)} different", problems))
    return results


def main():
    summary = FULL / "summary.csv"
    if not summary.exists():
        print("run make analyze first: results/full-summary/summary.csv is missing")
        return 1
    report_tex = REPORT.read_text()
    generated = [(name, path.read_text() if path.exists() else None) for name, path in GENERATED]
    results = check(report_tex, read_csv(summary), generated)
    for name, ok, message, problems in results:
        print(f"{'ok  ' if ok else 'DIFF'} {name}: {message}")
        for problem in problems:
            print(f"       {problem}")
    bad = sum(not ok for _, ok, _, _ in results)
    print("report matches the data" if not bad else f"{bad} table(s) differ from the data")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
