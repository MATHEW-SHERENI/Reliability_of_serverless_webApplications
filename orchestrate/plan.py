"""Experiment plan (M6): which runs to make, in which order, with which settings.

A plan file (experiments/*.json) names the configurations, conditions,
repetitions and seed. Everything here is pure: the same plan always gives the
same run order and the same settings, so the order can be checked before any
money is spent and reproduced afterwards.
"""
import json
import random

# Configuration name -> (MECH_RETRY, MECH_CACHE) flags of the Catalog.
CONFIGURATIONS = {
    "Base": (False, False),
    "Retry": (True, False),
    "Cache": (False, True),
    "Retry+Cache": (True, True),
}

# Condition -> fault settings of the dependency (midterm Table 4). C2 and C3
# need no fault: their stress is the arrival pattern, set in the k6 workload.
CONDITIONS = {
    "C1": {},
    "C2": {},
    "C3": {},
    "C4": {"FAULT_MODE": "transient", "FAULT_RATE": 0.10},
    "C5": {"FAULT_MODE": "delay", "FAULT_DELAY_MS": 300},
    "C6": {"FAULT_MODE": "delay", "FAULT_DELAY_MS": 1500},
    "C7": {"FAULT_MODE": "outage", "FAULT_OUTAGE_START_S": 60, "FAULT_OUTAGE_END_S": 120},
}

REQUIRED = ("name", "seed", "configurations", "conditions", "repetitions")


def validate(plan):
    """Raise ValueError if the plan names something unknown or is incomplete."""
    missing = [key for key in REQUIRED if key not in plan]
    if missing:
        raise ValueError(f"plan is missing {missing}")
    unknown = set(plan["configurations"]) - set(CONFIGURATIONS)
    unknown |= set(plan["conditions"]) - set(CONDITIONS)
    if unknown:
        raise ValueError(f"plan names unknown configurations or conditions: {sorted(unknown)}")
    if not plan["configurations"] or not plan["conditions"]:
        raise ValueError("plan needs at least one configuration and one condition")
    if type(plan["repetitions"]) is not int or plan["repetitions"] < 1:
        raise ValueError("repetitions must be a whole number of at least 1")
    if type(plan["seed"]) is not int:
        raise ValueError("seed must be a whole number")
    return plan


def load_plan(path):
    with open(path) as f:
        return validate(json.load(f))


def run_order(plan):
    """All runs of the plan in a seeded, randomized, interleaved order.

    Each repetition is one block containing every configuration-condition cell
    once, shuffled. Blocks follow each other, so all cells are spread over the
    whole session: a slow period on the shared cloud hits every configuration
    alike instead of all repetitions of one.
    """
    rng = random.Random(plan["seed"])
    order = []
    for repetition in range(1, plan["repetitions"] + 1):
        block = [(config, condition) for config in plan["configurations"]
                 for condition in plan["conditions"]]
        rng.shuffle(block)
        order += [{"configuration": config, "condition": condition, "repetition": repetition}
                  for config, condition in block]
    return order


def fault_settings(condition, run_id, run_start):
    """Environment settings for the dependency during one run."""
    settings = dict(CONDITIONS[condition])
    settings["RUN_ID"] = run_id
    settings["RUN_START"] = int(run_start)   # the outage window counts from here
    return settings
