# Shortcuts for the commands promised in midterm Section 5.3.
# IMPORTANT: each command line under a target must start with a TAB, not spaces.

TF := terraform -chdir=infra

.PHONY: init plan deploy destroy seed test

init:
	$(TF) init

plan:
	$(TF) plan

deploy:
	$(TF) apply

destroy:
	$(TF) destroy

seed:
	python orchestrate/seed_catalog.py --table $$($(TF) output -raw products_table)

test:
	python -m pytest -q
