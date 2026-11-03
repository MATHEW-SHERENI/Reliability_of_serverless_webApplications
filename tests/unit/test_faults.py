import random

import pytest

from faults import FaultConfig, decide_fault

RUN_START = 1000.0


def test_none_mode_never_faults():
    config = FaultConfig(mode="none")
    rng = random.Random(1)
    assert all(not decide_fault(RUN_START + i, rng, config).fail for i in range(1000))


def test_transient_fails_close_to_the_configured_rate():
    config = FaultConfig(mode="transient", rate=0.10)
    rng = random.Random(5360)
    failures = sum(decide_fault(RUN_START, rng, config).fail for _ in range(10_000))
    assert 900 <= failures <= 1100          # 10% of 10,000, with tolerance


def test_transient_extremes():
    rng = random.Random(1)
    assert not decide_fault(RUN_START, rng, FaultConfig(mode="transient", rate=0.0)).fail
    assert decide_fault(RUN_START, rng, FaultConfig(mode="transient", rate=1.0)).fail


def test_delay_adds_latency_but_never_fails():
    config = FaultConfig(mode="delay", delay_ms=300)
    fault = decide_fault(RUN_START, random.Random(1), config)
    assert fault.delay_ms == 300 and not fault.fail


def test_outage_window_edges():
    config = FaultConfig(mode="outage", outage_start_s=60, outage_end_s=120,
                         run_start=RUN_START)
    rng = random.Random(1)
    fails_at = lambda seconds: decide_fault(RUN_START + seconds, rng, config).fail
    assert not fails_at(59.9)
    assert fails_at(60.0)
    assert fails_at(119.9)
    assert not fails_at(120.0)


def test_config_is_read_from_environment():
    config = FaultConfig.from_env({"FAULT_MODE": "transient", "FAULT_RATE": "0.1"})
    assert config.mode == "transient" and config.rate == 0.1


@pytest.mark.parametrize("env", [
    {"FAULT_MODE": "transeint"},                   # typo in mode
    {"FAULT_MODE": "transient", "FAULT_RATE": "1.5"},
    {"FAULT_MODE": "delay", "FAULT_DELAY_MS": "-5"},
    {"FAULT_MODE": "outage", "FAULT_OUTAGE_START_S": "120", "FAULT_OUTAGE_END_S": "60"},
])
def test_bad_configuration_is_rejected(env):
    with pytest.raises(ValueError):
        FaultConfig.from_env(env)
