# Shortcuts for the commands promised in midterm Section 5.3.
# IMPORTANT: each command line under a target must start with a TAB, not spaces.

TF := terraform -chdir=infra
PY := .venv/bin/python

# Use the B10 profile unless the caller sets AWS_PROFILE; export it so boto3,
# the AWS CLI, and Terraform all use the same credentials.
AWS_PROFILE ?= B10
export AWS_PROFILE

.PHONY: init plan deploy destroy seed test test-integration ct5 smoke-load

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
