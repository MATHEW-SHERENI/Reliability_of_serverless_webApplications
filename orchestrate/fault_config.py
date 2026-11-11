"""Switch the dependency's fault settings and the Catalog's mechanism flags.

AWS replaces all environment variables on update, so the current set is read,
the new settings are merged in, and the result is written back. Every fault
switch starts from the healthy settings, so nothing from a previous mode
lingers. The orchestrator (M6) uses both functions before each run.
"""
import boto3

HEALTHY = {
    "FAULT_MODE": "none",
    "FAULT_RATE": "0",
    "FAULT_DELAY_MS": "0",
    "FAULT_OUTAGE_START_S": "0",
    "FAULT_OUTAGE_END_S": "0",
    "RUN_START": "0",
    "RUN_ID": "none",
}


def merged_variables(current, settings):
    """Return the new variable set: current values, reset to healthy, then settings."""
    unknown = set(settings) - set(HEALTHY)
    if unknown:
        raise ValueError(f"unknown fault settings: {sorted(unknown)}")
    variables = dict(current)
    variables.update(HEALTHY)
    variables.update({key: str(value) for key, value in settings.items()})
    return variables


def mechanism_variables(current, retry, cache):
    """Return the new variable set with both mechanism flags set to true or false."""
    variables = dict(current)
    variables["MECH_RETRY"] = "true" if retry else "false"
    variables["MECH_CACHE"] = "true" if cache else "false"
    return variables


def update_variables(function_name, region, change):
    """Read a function's variables, apply change(current), write them, and wait."""
    lam = boto3.client("lambda", region_name=region)
    current = lam.get_function_configuration(FunctionName=function_name)
    variables = change(current.get("Environment", {}).get("Variables", {}))
    lam.update_function_configuration(FunctionName=function_name,
                                      Environment={"Variables": variables})
    lam.get_waiter("function_updated_v2").wait(FunctionName=function_name)


def set_faults(function_name, region="us-east-2", **settings):
    """Apply fault settings to a function and wait until AWS has applied them."""
    update_variables(function_name, region,
                     lambda current: merged_variables(current, settings))


def set_mechanisms(function_name, retry, cache, region="us-east-2"):
    """Switch the Catalog's retry and cache flags and wait until AWS has applied them."""
    update_variables(function_name, region,
                     lambda current: mechanism_variables(current, retry, cache))
