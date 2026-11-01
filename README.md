# Reliability of Serverless Web Applications

CSCI 5360 Web Technologies, Fall 2026 (Saint Louis University). Blue Track project.
Author: Mathew Shereni

A controlled study of how two reliability mechanisms, retries with exponential
backoff and jitter, and in-function response caching, change the latency, error
rate, recovery time, and cost of an AWS Lambda Web API under cold starts, burst
concurrency, slow dependencies, timeouts, and outages.

The design is described in `docs/` (midterm deliverable).

## Repository layout

| Folder         | Contents                                      | Module |
|----------------|-----------------------------------------------|--------|
| `infra/`       | Terraform for all AWS resources               | M8     |
| `api/`         | Catalog API Lambda and reliability middleware | M1–M3  |
| `dep/`         | Simulated dependency with fault injector      | M4     |
| `load/`        | k6 workload scripts                           | M5     |
| `orchestrate/` | Experiment runner, catalog seeder             | M6     |
| `analysis/`    | Metrics, cost model, figures                  | M7     |
| `tests/`       | Correctness tests CT1–CT7                     | —      |

## Tool versions

| Tool      | Version |
|-----------|---------|
| Python    | 3.12.16 (via uv) |
| AWS CLI   | 2.31.35 |
| Terraform | 1.17.1  |
| k6        | 2.3.0   |
| uv        | 0.13.6  |

## Setup

```
uv venv && source .venv/bin/activate
uv pip install -r requirements-dev.txt
```

AWS: region `us-east-2`, CLI profile `b10`. Deployment steps will be added as the
infrastructure is built.

## Safety

- Never commit credentials, `.env` files, or Terraform state.
- Run `make destroy` after each session that leaves resources running.
