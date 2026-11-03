"""Fault injection decisions for the simulated dependency (module M4).

decide_fault() is a pure function: it only decides what to inject, given the
time, a random generator, and the configuration. The handler carries out the
decision (sleeping or returning 503), so the logic can be tested without
waiting, without AWS, and with predictable randomness.
"""
from dataclasses import dataclass

MODES = ("none", "transient", "delay", "outage")


@dataclass(frozen=True)
class FaultConfig:
    mode: str = "none"
    rate: float = 0.0          # transient: probability that a call fails
    delay_ms: int = 0          # delay: extra milliseconds on every call
    outage_start_s: float = 0  # outage: window start, seconds after run start
    outage_end_s: float = 0    # outage: window end (exclusive)
    run_start: float = 0       # epoch seconds when the experiment run began

    @classmethod
    def from_env(cls, env):
        """Read the configuration from environment variables and check it."""
        config = cls(
            mode=env.get("FAULT_MODE", "none"),
            rate=float(env.get("FAULT_RATE", "0")),
            delay_ms=int(env.get("FAULT_DELAY_MS", "0")),
            outage_start_s=float(env.get("FAULT_OUTAGE_START_S", "0")),
            outage_end_s=float(env.get("FAULT_OUTAGE_END_S", "0")),
            run_start=float(env.get("RUN_START", "0")),
        )
        config.validate()
        return config

    def validate(self):
        if self.mode not in MODES:
            raise ValueError(f"unknown FAULT_MODE {self.mode!r}; expected one of {MODES}")
        if not 0.0 <= self.rate <= 1.0:
            raise ValueError("FAULT_RATE must be between 0 and 1")
        if self.delay_ms < 0:
            raise ValueError("FAULT_DELAY_MS must not be negative")
        if self.outage_end_s < self.outage_start_s:
            raise ValueError("FAULT_OUTAGE_END_S must not be before FAULT_OUTAGE_START_S")


@dataclass(frozen=True)
class Fault:
    fail: bool       # return 503 instead of a real answer
    delay_ms: int    # wait this long before answering


NO_FAULT = Fault(fail=False, delay_ms=0)


def decide_fault(now, rng, config):
    """Decide what to inject for one call. Pure: no sleeping, no clock, no AWS."""
    if config.mode == "transient":
        return Fault(fail=rng.random() < config.rate, delay_ms=0)
    if config.mode == "delay":
        return Fault(fail=False, delay_ms=config.delay_ms)
    if config.mode == "outage":
        elapsed = now - config.run_start
        inside = config.outage_start_s <= elapsed < config.outage_end_s
        return Fault(fail=inside, delay_ms=0)
    return NO_FAULT
