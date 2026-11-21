"""make check-report: every table of the report against regenerated results."""
from analysis.check_report import (agrees, check, check_effects, check_read_p95, data_rows,
                                   missing_rows, shown)

GENERATED = r"""\begin{tabular}{@{}lrr@{}}
\toprule
\textbf{Configuration} & \textbf{C1} & \textbf{C4} \\
 & (\%) & (\%) \\
\midrule
\multicolumn{3}{@{}l}{\emph{a section label}} \\
Base & 100 (15/15) & 0 (0/15) \\
Retry & 100 (15/15) & 40 (6/15) \\
\midrule
Noise floor & \multicolumn{2}{c}{0.00} \\
\bottomrule
\end{tabular}
"""

REPORT = r"""Some text & with an ampersand.
\begin{table}[htbp]
\begin{tabular}{@{}lrr@{}}
Base  &  100 (15/15) & 0 (0/15) \\
Retry & 100 (15/15) & 0 (0/15) \\
\end{tabular}
\label{tab:slo}
\end{table}
\begin{table}[htbp]
\begin{tabular}{@{}llrrrrr@{}}
\textbf{Cond.} & \textbf{Configuration} & \textbf{Errors (\%)} & \textbf{p95 (ms)} & x & y & z \\
\midrule
C4 & Base  & 9.98 (9.65--10.37) & 46.6 (44.5--52.2) & 2.02 (2.01--2.03) & -- & -- \\
C6 & Cache & 100 (100--100) & 1139 (1135--1140) & -- & 0.0 & -- \\
\end{tabular}
\label{tab:effects}
\end{table}
\begin{table}[htbp]
\begin{tabular}{@{}lrrrrrrr@{}}
Base & 42.7 & 1,391 & 50.0 & 44.2 & 341.0 & 1,138 & 44.6 \\
Retry & 44.7 & 1,404 & 47.9 & 159.4\,$\uparrow$ & 341.1 & 3,165\,$\uparrow$ & 1,071\,$\uparrow$ \\
\end{tabular}
\label{tab:readp95}
\end{table}
"""


def cell(condition, configuration, **values):
    return {"condition": condition, "configuration": configuration, **values}


def effects_cells(base_error_max=0.1037):
    return [
        cell("C4", "Base", error_rate_median=0.0998, error_rate_min=0.0965,
             error_rate_max=base_error_max, p95_ms_median=46.6, p95_ms_min=44.5,
             p95_ms_max=52.2, cost_per_1000_usd_median=0.00202,
             cost_per_1000_usd_min=0.00201, cost_per_1000_usd_max=0.00203,
             hit_ratio_median=None, stale_rate_median=None),
        cell("C6", "Cache", error_rate_median=1.0, error_rate_min=1.0, error_rate_max=1.0,
             p95_ms_median=1138.8, p95_ms_min=1135.2, p95_ms_max=1140.0,
             cost_per_1000_usd_median=None, hit_ratio_median=0.0, stale_rate_median=None),
    ]


def read_cells(retry_c4=159.41):
    medians = {"Base": (42.66, 1390.93, 49.97, 44.16, 340.98, 1138.32, 44.57),
               "Retry": (44.69, 1404.38, 47.91, retry_c4, 341.08, 3164.91, 1071.02)}
    return [cell(f"C{k + 1}", name, read_p95_ms_median=v)
            for name, values in medians.items() for k, v in enumerate(values)]


def test_data_rows_leave_headers_units_and_labels_out():
    assert data_rows(GENERATED) == [r"Base & 100 (15/15) & 0 (0/15) \\",
                                    r"Retry & 100 (15/15) & 40 (6/15) \\",
                                    r"Noise floor & \multicolumn{2}{c}{0.00} \\"]


def test_a_changed_row_is_reported_and_spacing_does_not_matter():
    total, missing = missing_rows(GENERATED, REPORT)
    # Base matches although the report has double spaces; Retry's C4 is 0 there, 40 here
    # and the noise-floor row is not in this report at all
    assert (total, missing) == (3, [r"Retry & 100 (15/15) & 40 (6/15) \\",
                                    r"Noise floor & \multicolumn{2}{c}{0.00} \\"])


def test_shown_numbers():
    assert shown(r"9.98 (9.65--10.37)") == ["9.98", "9.65", "10.37"]
    assert shown(r"3,165\,$\uparrow$") == ["3165"]
    assert shown(" -- ") == [None]


def test_rounding_to_the_decimals_shown():
    assert agrees("9.98", 9.98) and agrees("46.6", 46.649) and agrees("1139", 1138.8)
    assert not agrees("46.6", 46.66)               # would be shown as 46.7
    assert not agrees("2.02", None) and agrees(None, None)


def test_effects_table_numbers():
    # C4 Base: 3 + 3 + 3 numbers and two "--"; C6 Cache: 3 + 3, "--", 0.0, "--" -> 20
    checked, problems = check_effects(REPORT, effects_cells())
    assert (checked, problems) == (20, [])
    # an error-rate maximum of 10.50 % no longer rounds to the 10.37 shown
    checked, problems = check_effects(REPORT, effects_cells(base_error_max=0.1050))
    assert len(problems) == 1 and problems[0].startswith("C4 Base, column 1")


def test_read_p95_table_numbers():
    assert check_read_p95(REPORT, read_cells()) == (14, [])        # 2 rows x 7 conditions
    _, problems = check_read_p95(REPORT, read_cells(retry_c4=160.0))   # 159.4 shown
    assert problems == ["Retry, column 4: report ['159.4'], data [160.0]"]


def test_a_missing_file_fails_the_check():
    # one cell per condition and configuration, as in summary.csv
    merged = {}
    for c in effects_cells() + read_cells():
        merged.setdefault((c["condition"], c["configuration"]), {}).update(c)
    results = check(REPORT, list(merged.values()),
                    [("output 1", GENERATED), ("output 5", None)])
    verdicts = {name: ok for name, ok, _, _ in results}
    assert verdicts == {"output 1": False, "output 5": False, "effects table": True,
                        "read p95 table": True}
