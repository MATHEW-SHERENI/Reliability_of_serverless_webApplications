"""Experiment plan (M6): which runs to make, in which order, with which settings.

A plan file (experiments/*.json) names the configurations, conditions,
repetitions and seed. Everything here is pure: the same plan always gives the
same run order and the same settings, so the order can be checked before any
money is spent and reproduced afterwards.
"""
import json
import random

# Configuration name -> (MECH_RETRY, MECH_CACHE, MECH_BREAKER) flags of the Catalog.
# The first four are the full experiment's; the breaker ones belong to task T4.
CONFIGURATIONS = {
    "Base": (False, False, False),
    "Retry": (True, False, False),
    "Cache": (False, True, False),
    "Retry+Cache": (True, True, False),
    "Breaker": (False, False, True),
    "Retry+Breaker": (True, False, True),
    "Cache+Breaker": (False, True, True),
    "Retry+Cache+Breaker": (True, True, True),
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

# Seconds of load per run (warm-up + measured phase, from load/workload.js) and a
# measured allowance for switching settings, collecting logs and uploading files
# (the pilot's 8 runs took about 33 minutes: 210 s of load plus about 40 s each).
LOAD_S = {"C1": 210, "C2": 41, "C3": 330, "C4": 210, "C5": 210, "C6": 210, "C7": 210}
OVERHEAD_S = 60


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
    control = plan.get("control")
    if control is not None:
        if (not isinstance(control, dict)
                or control.get("configuration") not in plan["configurations"]
                or control.get("condition") not in plan["conditions"]):
            raise ValueError("control must name a configuration and a condition of the plan")
    return plan


def load_plan(path):
    with open(path) as f:
        return validate(json.load(f))


def run_order(plan, session=None):
    """All runs of the plan (or of one session) in a seeded, randomized, interleaved order.

    Each repetition is one block containing every configuration-condition cell
    once, shuffled, so a slow period on the shared cloud hits every
    configuration alike instead of all repetitions of one. A plan with a
    "control" cell adds one more run of that cell to every block, marked
    control: the A/A comparison (midterm Section 5), whose difference from the
    regular run of the same cell shows how much two identical runs differ.

    session: run only that repetition's block (1 = the first). The order of a
    block does not depend on which sessions are run, because every block is
    drawn from the same seeded sequence.
    """
    rng = random.Random(plan["seed"])
    control = plan.get("control")
    order = []
    for repetition in range(1, plan["repetitions"] + 1):
        block = [(config, condition, False) for config in plan["configurations"]
                 for condition in plan["conditions"]]
        if control:
            block.append((control["configuration"], control["condition"], True))
        rng.shuffle(block)
        order += [{"configuration": config, "condition": condition, "repetition": repetition,
                   "control": is_control} for config, condition, is_control in block]
    if session is None:
        return order
    if type(session) is not int or not 1 <= session <= plan["repetitions"]:
        raise ValueError(f"session must be 1 to {plan['repetitions']}")
    return [run for run in order if run["repetition"] == session]


def estimated_seconds(runs):
    """Rough wall-clock time of a list of runs: load time plus a fixed allowance per run."""
    return sum(LOAD_S[run["condition"]] + OVERHEAD_S for run in runs)


def fault_settings(condition, run_id, run_start):
    """Environment settings for the dependency during one run."""
    settings = dict(CONDITIONS[condition])
    settings["RUN_ID"] = run_id
    settings["RUN_START"] = int(run_start)   # the outage window counts from here
    return settings
