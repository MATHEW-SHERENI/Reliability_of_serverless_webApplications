"""Switch the dependency's fault settings without losing its other variables.

AWS replaces all environment variables on update, so the current set is read,
the fault settings are merged in, and the result is written back. Every switch
starts from the healthy settings, so nothing from a previous mode lingers.
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


def set_faults(function_name, region="us-east-2", **settings):
    """Apply fault settings to a function and wait until AWS has applied them."""
    lam = boto3.client("lambda", region_name=region)
    current = lam.get_function_configuration(FunctionName=function_name)
    variables = merged_variables(current.get("Environment", {}).get("Variables", {}), settings)
    lam.update_function_configuration(FunctionName=function_name,
                                      Environment={"Variables": variables})
    lam.get_waiter("function_updated_v2").wait(FunctionName=function_name)
