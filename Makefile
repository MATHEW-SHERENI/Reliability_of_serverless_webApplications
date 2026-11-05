# Shortcuts for the commands promised in midterm Section 5.3.
# IMPORTANT: each command line under a target must start with a TAB, not spaces.

TF := terraform -chdir=infra
PY := .venv/bin/python

# Use the B10 profile unless the caller sets AWS_PROFILE; export it so boto3,
# the AWS CLI, and Terraform all use the same credentials.
AWS_PROFILE ?= B10
export AWS_PROFILE

.PHONY: init plan deploy destroy seed test test-integration ct5

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
