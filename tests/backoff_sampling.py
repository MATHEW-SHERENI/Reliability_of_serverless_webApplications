"""Day 11 review check (midterm Section 7): sample 10,000 backoff delays.

The delays come from the real call_with_retry, driven by a dependency that
always fails and a fake clock, so nothing waits. For each retry n the script
prints how many delays fell outside [0, ceiling], their range and mean
(full jitter should average half the ceiling), and how they spread over ten
equal bins (uniform jitter puts about a tenth in each).
Run with: .venv/bin/python tests/backoff_sampling.py
"""
import random
import statistics
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "api")]

import reliability  # noqa: E402
from dep_client import DependencyServerError  # noqa: E402

TARGET = 10_000


class FakeClock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def always_fails(timeout_s):
    raise DependencyServerError("injected fault", status=503)


def main():
    by_retry = {1: [], 2: [], 3: []}
    rng = random.Random(5360)
    while sum(len(v) for v in by_retry.values()) < TARGET:
        clock = FakeClock()
        try:
            reliability.call_with_retry(always_fails, clock, rng)
        except DependencyServerError:
            pass
        for n, delay in enumerate(clock.sleeps, start=1):
            by_retry[n].append(delay)

    total = sum(len(v) for v in by_retry.values())
    outside = 0
    print(f"{total} delays sampled")
    for n, delays in by_retry.items():
        ceiling = reliability.backoff_ceiling_s(n)
        bad = sum(not 0 <= d <= ceiling for d in delays)
        outside += bad
        bins = [0] * 10
        for d in delays:
            bins[min(int(d / ceiling * 10), 9)] += 1
        print(f"retry {n}: ceiling {ceiling * 1000:.0f} ms, n={len(delays)}, "
              f"outside={bad}, min {min(delays) * 1000:.1f} ms, "
              f"max {max(delays) * 1000:.1f} ms, mean {statistics.mean(delays) * 1000:.1f} ms "
              f"(expected {ceiling * 500:.0f})")
        print(f"         bins: {bins}")
    print("PASS" if outside == 0 else "FAIL")
    return 0 if outside == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
