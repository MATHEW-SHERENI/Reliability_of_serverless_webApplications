# Shortcuts for the commands promised in midterm Section 5.3.
# IMPORTANT: each command line under a target must start with a TAB, not spaces.

TF := terraform -chdir=infra
PY := .venv/bin/python

# Use the B10 profile unless the caller sets AWS_PROFILE; export it so boto3,
# the AWS CLI, and Terraform all use the same credentials.
AWS_PROFILE ?= B10
export AWS_PROFILE

.PHONY: init plan deploy destroy seed test test-integration ct5 smoke-load data-init data-deploy ec2-run experiment metrics plot summary plan-check

init:
	$(TF) init

plan:
	$(TF) plan

deploy:
	$(TF) apply

destroy:
	$(TF) destroy

seed:
	$(PY) orchestrate/seed_catalog.py --table $$($(TF) output -raw products_table)

test:
	$(PY) -m pytest -q

test-integration:
	$(PY) -m pytest -m integration -v

ct5:
	$(PY) tests/integration/ct5_fault_calibration.py

# A 25-second, 5 req/s run of the k6 workload from this computer: checks the
# script and the deployed API, not performance (real runs use the EC2 load host).
smoke-load:
	mkdir -p results
	rm -f results/smoke.jsonl
	k6 run --quiet --log-format=raw --console-output=results/smoke.jsonl \
	  -e BASE_URL=$$($(TF) output -raw api_url) -e CONDITION=C1 -e CONFIG=Base \
	  -e RUN_ID=smoke -e SCALE=0.1 -e WARMUP_S=5 -e MAIN_S=20 \
	  -e SUMMARY=results/smoke-summary.json load/workload.js
	$(PY) load/check_records.py results/smoke.jsonl

# Results bucket: a separate stack that `make destroy` never touches.
# Run data-init once, then data-deploy once; there is no data-destroy on purpose.
data-init:
	terraform -chdir=infra/data init

data-deploy:
	terraform -chdir=infra/data apply

# One full-size C1 run of the deployed configuration from the EC2 load host.
ec2-run:
	$(PY) orchestrate/loadhost.py --condition C1 --config Base

# Run every run of a plan file through the orchestrator (M6), e.g.
#   make experiment PLAN=experiments/pilot.json
PLAN ?= experiments/smoke.json
experiment:
	$(PY) orchestrate/experiment.py $(PLAN) $(if $(SESSION),--session $(SESSION))

# Print a plan's run order and estimated duration without touching AWS, e.g.
#   make plan-check PLAN=experiments/full.json SESSION=1
plan-check:
	$(PY) orchestrate/experiment.py $(PLAN) --dry-run $(if $(SESSION),--session $(SESSION))

# Metrics (M7) of one run or of every valid run of an experiment, e.g.
#   make metrics RUN=results/smoke-20261115T051707Z-001-C1-Base-r1-a1
metrics:
	$(PY) analysis/metrics.py $(RUN)

# One figure with every metric of one run (after make metrics), e.g.
#   make plot RUN=results/smoke-20261115T051707Z-001-C1-Base-r1-a1
plot:
	$(PY) analysis/plot_run.py $(RUN)

# Summary of an experiment (after make metrics on it): median and range per cell,
# LaTeX table and comparison figure, e.g.
#   make summary EXP=results/pilot-20261117T034701Z
# or, combining the five sessions of the full experiment:
#   make summary EXP="results/full-s1-* results/full-s2-*" OUT=results/full-summary
summary:
	$(PY) analysis/summarize.py $(EXP) $(if $(OUT),--out $(OUT))
