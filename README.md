# Reliability of Serverless Web Applications

CSCI 5360 Web Technologies, Fall 2026 (Saint Louis University). Blue Track project.
Author: Mathew Shereni

A controlled study of how two reliability mechanisms, retries with exponential
backoff and jitter, and in-function response caching, change the latency, error
rate, recovery time, and cost of an AWS Lambda Web API under cold starts, burst
concurrency, slow dependencies, timeouts, and outages.

The design is in `docs/midterm_design.pdf`. The final report is written as the
work progresses, in `report/final_report.tex`.

## Status

| Task | Status |
|------|--------|
| T1 Testbed: Catalog API, dependency with fault injector, Terraform | Done, tag `v0.1-testbed` (CT1, CT2, CT5 pass) |
| T2 Reliability mechanisms: retries and in-function cache | Done except two CT4 handler tests (CT3, CT4 unit tests pass; flags switch configurations live) |
| T3 Measurement pipeline and evaluation (140 runs) | Done, tag `v0.3-results`: 5 sessions, 145 runs (140 + 5 A/A controls), all valid at the first attempt; `make reproduce` regenerates every table and figure from the raw files and checks the report |
| T4 Circuit breaker (ambitious) | Done, tag `v0.3-results`: state-transition tests pass (`tests/unit/test_breaker.py`); C7 with the breaker in four configurations, 5 repetitions, 20 valid runs (`experiments/breaker.json`, `make h3`) |
| T5 Request deduplication (if time permits) | Planned |

## Architecture

```
client --HTTPS--> API Gateway HTTP API --> Catalog Lambda (api/) --SigV4--> Dependency Lambda (dep/) --> DynamoDB
                  GET  /products/{id}                            function URL,             b10-products
                  POST /orders                                   IAM auth only             b10-orders
```

- **Catalog Lambda** (`api/app.py`): validates input, calls the dependency with a
  per-attempt timeout, maps failures to HTTP statuses, and adds diagnostic
  headers (`X-Cold-Start`, `X-Env-Id`, `X-Request-Id`, `X-Attempts`, `X-Cache`,
  `X-Data-Version`) and one JSON log line per request.
- **Dependency Lambda** (`dep/handler.py`): reads products and reserves stock in
  a conditional DynamoDB transaction. Its fault injector (`dep/faults.py`) can
  add transient 503s, a fixed delay, or a timed outage, and logs every injected
  fault as ground truth.
- All resources are defined in Terraform (`infra/`) in us-east-2 and tagged
  `Project=b10`.

## Prerequisites

- An AWS account with a CLI profile named `B10` (region us-east-2). To use another
  profile name, change `aws_profile` in `infra/variables.tf` and run make with
  `AWS_PROFILE=<name>`.
- A Lambda concurrency quota of at least 1,000 in us-east-2 (the stack reserves 800).
- Terraform 1.17 or later, and [uv](https://docs.astral.sh/uv/). uv installs
  Python 3.12 from `.python-version` if it is missing.

## Quick start

```
uv venv
uv pip install -r requirements-dev.txt
make init                # download the pinned Terraform providers
make deploy              # read the plan, then type yes
make seed                # load 1,000 products
make test                # unit tests, no AWS needed
make test-integration    # CT1 against the deployed stack
make destroy             # remove everything when finished
```

The virtual environment does not need to be activated: the Makefile calls
`.venv/bin/python` directly.

## Make targets

| Target | What it does |
|--------|--------------|
| `make init` | `terraform init` in `infra/` |
| `make plan` | Show what Terraform would change |
| `make deploy` | Create or update all AWS resources |
| `make destroy` | Delete all AWS resources |
| `make seed` | Write the 1,000-product catalog (seed 5360) |
| `make test` | Unit tests (205), no AWS access |
| `make test-integration` | Integration tests that need the deployed stack (CT1) |
| `make ct5` | Fault injector calibration, about 4 minutes (CT5) |
| `make smoke-load` | 25-second, 10% k6 run from this computer (checks script and API) |
| `make data-init`, `make data-deploy` | Create the versioned results bucket once (separate stack, never destroyed by `make destroy`) |
| `make ec2-run` | One full C1 run of the deployed configuration from the EC2 load host |
| `make experiment PLAN=experiments/<plan>.json` | Run every run of a plan through the orchestrator (M6); default `experiments/smoke.json` |
| `make experiment PLAN=experiments/breaker.json` | Task T4: the four breaker configurations in C7, 5 repetitions (20 runs, about 1 h 30 min); needs `make deploy` after the breaker code change |
| `make experiment PLAN=experiments/full.json SESSION=1` | Run one session (one repetition, 29 runs including the A/A control, about 2 hours) of the full experiment |
| `make plan-check PLAN=experiments/<plan>.json [SESSION=n]` | Print the run order and estimated duration without touching AWS |
| `make analyze` | Every table and figure of the full experiment from its raw files: outputs 1-5 (verdict matrix, RQ1 table, C7 time series, C3 figure, cost table), the A/A noise floor, and the judged effects (`effects.csv`) in `results/full-summary/`. `make analyze-quick` reuses each run's metrics and takes seconds |
| `make h3` | Task T4 (H3): each breaker configuration against its partner without the breaker in C7 (range rule and noise floor), how fast each environment's breaker opened and closed, and reads served by the stale fallback; `h3.tex`, `degraded.tex`, `h3_c7.png`, `h3_opening.png` in `results/h3/` |
| `make reproduce` | Everything at once, from the raw run files and without AWS: `make analyze`, the pilot and breaker experiments, `make h3`, the report's figures copied into `report/figures/`, then `make check-report` (T3 completion criterion) |
| `make check-report` | Every table of the report against the regenerated results: rows the analysis writes as LaTeX must appear in `report/final_report.tex`, and the effects and read-p95 tables must show the regenerated values rounded as printed; fails on any difference. Run after `make analyze`, `make results PLAN=experiments/breaker.json` and `make h3` |
| `make results PLAN=full` | Every figure and table of every experiment of a plan (metrics, one figure per run, each experiment's summary) and the plan's combined summary in `results/<plan>-summary/`, without AWS. The orchestrator does this automatically at the end of each experiment |
| `make metrics RUN=results/<folder>` | Metrics (M7) of one run, or of every valid run of an experiment; writes `metrics.csv` (and `windows.csv`, `buckets.csv` per run) |
| `make plot RUN=results/<run_id>` | One figure with every metric of a run (`figure.png`, `figure.pdf`): latency and throughput over time, SLO per minute, failures by cause, all metrics, and cost by part |
| `make summary EXP=results/<experiment_id>` | Median and range of every metric per cell (`summary.csv`), a LaTeX table (`summary.tex`), and a figure comparing the four configurations per condition (`comparison.png`, `comparison.pdf`), and the SLO verdict matrix, output 1 (`slo_matrix.tex`, `slo_matrix.png`, `slo_matrix.pdf`). Several experiments: `EXP="results/full-s1-* results/full-s2-*" OUT=results/full-summary` |

## Tests

| Test | Type | What it shows |
|------|------|---------------|
| Unit tests (`tests/unit/`) | pytest, offline | Seeder, fault logic, dependency client, Catalog handler (including CT2, invalid input), fault switching |
| T4 breaker (`tests/unit/test_breaker.py`) | pytest, offline | With a fake clock: 5 failures in a row open the breaker, a success resets the count, 15 s cooldown, one half-open probe that closes or reopens it; open breaker answers 503 with `X-Degraded` or an expired cache entry (`X-Cache: STALE`) without calling the dependency; 404 counts as an answer; one retried request counts once |
| CT6 (`tests/unit/test_ct6_metrics.py`) | pytest, offline | A hand-built run of 20 requests with one outage: p50/p95/p99, error rate, recovery time and cost equal the values computed by hand |
| CT1 (`tests/integration/test_ct1_workflow.py`) | pytest, marked `integration` | A product read and an order through the deployed API; stock drops by exactly 2 |
| CT5 (`tests/integration/ct5_fault_calibration.py`) | script | 10% faults give 9-11% failures; +300 ms delay adds 270-330 ms to median dependency latency; outage failures only inside the window |

## Fault injection

The dependency reads its fault settings from environment variables at cold start
(`FAULT_MODE` = `none`, `transient`, `delay`, or `outage`, plus `FAULT_RATE`,
`FAULT_DELAY_MS`, `FAULT_OUTAGE_START_S`, `FAULT_OUTAGE_END_S`, `RUN_START`,
`RUN_ID`). Switch them with `orchestrate/fault_config.py`, which keeps the other
variables and resets earlier fault settings:

```
.venv/bin/python -c "from orchestrate.fault_config import set_faults; \
set_faults('b10-dependency', FAULT_MODE='transient', FAULT_RATE=0.1)"
.venv/bin/python -c "from orchestrate.fault_config import set_faults; \
set_faults('b10-dependency')"    # back to healthy
```

## Repository layout

| Folder         | Contents                                      | Module |
|----------------|-----------------------------------------------|--------|
| `infra/`       | Terraform for all AWS resources               | M8     |
| `api/`         | Catalog API Lambda and reliability middleware | M1-M3  |
| `dep/`         | Simulated dependency with fault injector      | M4     |
| `load/`        | k6 workload scripts                           | M5     |
| `orchestrate/` | Experiment runner, catalog seeder, fault switching | M6 |
| `analysis/`    | Metrics, cost model, figures                  | M7     |
| `tests/`       | Unit tests and correctness tests CT1-CT7      | -      |
| `report/`      | Final report (LaTeX)                          | -      |
| `docs/`        | Midterm design                                | -      |

## Tool versions

| Tool | Version |
|------|---------|
| Python | 3.12.16 (via uv) |
| AWS CLI | 2.31.35 |
| Terraform | 1.17.1 |
| AWS provider for Terraform | 6.71.0 (pinned in `infra/.terraform.lock.hcl`) |
| Archive provider for Terraform | 2.8.1 (pinned) |
| k6 | 2.3.0 |
| uv | 0.13.6 |

## Cost and safety

- Everything is billed per request (Lambda, API Gateway, DynamoDB on demand), so a
  deployed but idle stack costs almost nothing; still, run `make destroy` at the
  end of every session.
- Terraform state is stored locally in `infra/`. Deploy and destroy from the same
  folder; a second clone has its own empty state and does not know about
  resources the first one created.
- Never commit credentials, `.env` files, `*.tfvars`, or Terraform state
  (all excluded by `.gitignore`).
