# Instructions for coding agents

This repository is a graduate course project (CSCI 5360, Saint Louis University):
a controlled experiment on how retries and an in-function cache change the
latency, error rate, recovery time and cost of an AWS Lambda web API. The design
is fixed in `docs/midterm_design.pdf`; do not change the research design.

## Layout

| Folder | Contents |
|--------|----------|
| `api/` | Catalog Lambda (`app.py`) and the retry policy and cache (`reliability.py`) |
| `dep/` | Dependency Lambda with the fault injector |
| `load/` | k6 workload (`workload.js`) |
| `orchestrate/` | Experiment runner, plan files, price updater, log export |
| `analysis/` | Metrics per run (`metrics.py`), run figure (`plot_run.py`) |
| `tests/unit/` | Offline tests; `test_ct6_metrics.py` is correctness test CT6 |
| `tests/integration/` | Tests that need the deployed AWS stack (never run them) |
| `infra/` | Terraform |
| `report/` | Final report (LaTeX) |

## Rules

1. Work only on the current branch. Never commit to or push `main`.
2. Never run anything that touches AWS: no `make deploy`, `make destroy`,
   `make seed`, `make experiment`, `make test-integration`, `terraform`, or the
   AWS CLI. They cost money and change shared state.
3. Python 3.12 is the project version (`.venv/bin/python`). Code must also run
   on 3.13. Use only the standard library and the packages already pinned in
   `requirements-dev.txt` (matplotlib is there). Ask before adding a package.
4. Run `make test` before you finish. All tests must pass. Report the exact
   pass count.
5. Do not change existing tests or their expected values. In particular, never
   edit the expected values in `tests/unit/test_ct6_metrics.py` or the CT3/CT4
   tests in `tests/unit/test_reliability.py`. Add new tests instead.
6. Do not write the two CT4 handler tests marked TODO in
   `tests/unit/test_app.py`. The student writes those himself.
7. Do not edit `report/final_report.tex`, `docs/`, or `infra/`.
8. Expected values in new tests must come from the task description or from a
   hand computation written in a comment next to the assertion, never from
   running the code under test and copying its output.
9. Style: short functions, a docstring on each public function saying what it
   returns and why, plain English comments that explain reasons rather than
   restate code. Match the style of `analysis/metrics.py`.
10. Figures: serif font, black or dark grey text, thin marks, one y-axis per
    panel (never two), a legend when there is more than one series, and these
    fixed colours per configuration: Base `#2a78d6`, Retry `#eb6834`,
    Cache `#1baf7a`, Retry+Cache `#eda100`.
11. When done, list every file you changed and summarise each change in one
    line.

## Definitions (from the midterm, do not change)

- Only the measured phase counts (`ph != "warmup"` in k6 records).
- A request fails if its status is 0 (no answer), 429, or 500 and above.
- Percentiles use the nearest rank: the value at position ceil(p * n / 100) of
  the sorted values.
- A 1-minute window meets the SLO if p95 <= 500 ms and error rate <= 1%.
- Per cell of the experiment (configuration x condition), report the median and
  the range (minimum to maximum) of the five per-run values. A difference
  between two configurations counts as an effect only if their ranges do not
  overlap.
