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
| T2 Reliability mechanisms: retries and in-function cache | Next |
| T3 Measurement pipeline and evaluation (140 runs) | Planned |
| T4 Circuit breaker (ambitious) | Planned |
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
| `make test` | Unit tests (46), no AWS access |
| `make test-integration` | Integration tests that need the deployed stack (CT1) |
| `make ct5` | Fault injector calibration, about 4 minutes (CT5) |

## Tests

| Test | Type | What it shows |
|------|------|---------------|
| Unit tests (`tests/unit/`) | pytest, offline | Seeder, fault logic, dependency client, Catalog handler (including CT2, invalid input), fault switching |
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
