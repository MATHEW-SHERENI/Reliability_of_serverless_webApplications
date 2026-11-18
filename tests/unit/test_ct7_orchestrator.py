"""CT7 (midterm Table 5): failed collection is caught by the orchestrator (M6).

The orchestrator runs here with the cloud replaced by a scripted backend, so
k6 dropping iterations, an S3 file failing to arrive, a crashed run and a lost
manifest can be produced on demand. Expected: each such run is marked invalid,
queued again, and left out of the runs the analysis may use.
"""
import pytest

from orchestrate.experiment import judge, run_experiment, valid_runs

PLAN = {"name": "t", "seed": 5360, "configurations": ["Base", "Retry"],
        "conditions": ["C1"], "repetitions": 1}
GOOD = {"missing_files": [], "summary": {"dropped_iterations": 0, "requests": 10500},
        "cpu_max": 9}


class ScriptedBackend:
    """Plays a list of outcomes, one per run: a result dict, or an exception to raise."""

    def __init__(self, outcomes, fail_manifest_for=()):
        self.outcomes = list(outcomes)
        self.fail_manifest_for = set(fail_manifest_for)
        self.executed, self.saved, self.restored, self.experiment = [], [], 0, None

    def execute(self, cell, run_id, plan):
        self.executed.append(run_id)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)

    def save_manifest(self, manifest):
        if len(self.saved) + 1 in self.fail_manifest_for:
            self.saved.append(None)
            raise OSError("S3 put failed")
        self.saved.append(manifest["run_id"])

    def restore(self):
        self.restored += 1

    def save_experiment(self, experiment_id, plan, manifests):
        self.experiment = valid_runs(manifests)


def run(backend, **kwargs):
    return run_experiment(PLAN, backend, "t", say=lambda *a: None, **kwargs)


def test_ct7_dropped_iterations_mark_the_run_invalid_and_queue_it_again():
    dropped = {**GOOD, "summary": {"dropped_iterations": 23, "requests": 10479}}
    backend = ScriptedBackend([dropped, GOOD, GOOD])
    manifests = run(backend)
    assert [m["valid"] for m in manifests] == [False, True, True]
    assert manifests[0]["reason"] == "23 dropped iterations"
    assert manifests[2]["attempt"] == 2                  # the failed cell came back at the end
    assert manifests[2]["configuration"] == manifests[0]["configuration"]


def test_ct7_a_missing_s3_file_marks_the_run_invalid():
    lost = {**GOOD, "missing_files": ["records.jsonl.gz"]}
    manifests = run(ScriptedBackend([lost, GOOD, GOOD]))
    assert manifests[0]["valid"] is False
    assert "records.jsonl.gz" in manifests[0]["reason"]


def test_ct7_invalid_runs_never_reach_the_analysis():
    dropped = {**GOOD, "summary": {"dropped_iterations": 1, "requests": 10499}}
    backend = ScriptedBackend([dropped, GOOD, GOOD])
    manifests = run(backend)
    used = {m["run_id"] for m in backend.experiment}
    assert manifests[0]["run_id"] not in used
    assert used == {manifests[1]["run_id"], manifests[2]["run_id"]}


def test_ct7_a_crashed_run_is_recorded_and_the_experiment_goes_on():
    backend = ScriptedBackend([RuntimeError("SSM timed out"), GOOD, GOOD])
    manifests = run(backend)
    assert manifests[0]["valid"] is False and "SSM timed out" in manifests[0]["reason"]
    assert len(valid_runs(manifests)) == 2


def test_ct7_a_manifest_that_cannot_be_stored_makes_the_run_invalid():
    backend = ScriptedBackend([GOOD, GOOD, GOOD], fail_manifest_for={1})
    manifests = run(backend)
    assert manifests[0]["valid"] is False and "manifest not saved" in manifests[0]["reason"]
    assert len(valid_runs(manifests)) == 2


def test_ct7_gives_up_after_three_attempts():
    dropped = {**GOOD, "summary": {"dropped_iterations": 5, "requests": 10000}}
    backend = ScriptedBackend([dropped, GOOD, dropped, dropped])
    manifests = run(backend)
    assert len(manifests) == 4                           # 1 + 1 + two more tries of the bad cell
    assert len(valid_runs(manifests)) == 1


def test_the_stack_is_restored_even_when_the_experiment_crashes():
    class Interrupted(BaseException):
        pass
    backend = ScriptedBackend([Interrupted()])
    with pytest.raises(Interrupted):
        run(backend)
    assert backend.restored == 1


@pytest.mark.parametrize("result, reason", [
    ({**GOOD, "cpu_max": 71}, "load-host CPU 71% above 70%"),
    ({**GOOD, "summary": None}, "no k6 summary or CPU record"),
    (GOOD, "valid"),
])
def test_judge_applies_the_midterm_rules(result, reason):
    assert judge(result)[1] == reason


def test_the_aa_control_is_kept_apart_from_the_regular_run():
    from orchestrate.experiment import run_id_for
    base = {"configuration": "Base", "condition": "C1", "repetition": 1, "valid": True}
    manifests = [{**base, "control": False}, {**base, "control": True},
                 {"configuration": "Retry", "condition": "C1", "repetition": 1, "valid": True}]
    assert len(valid_runs(manifests)) == 3                   # the control does not replace Base
    assert run_id_for("x", 7, {**base, "control": True}, 1) == "x-007-C1-BaseAA-r1-a1"
    assert run_id_for("x", 8, {**base, "control": False}, 2) == "x-008-C1-Base-r1-a2"


def test_a_session_runs_only_its_block():
    plan = {**PLAN, "repetitions": 2, "control": {"configuration": "Base", "condition": "C1"}}
    backend = ScriptedBackend([GOOD] * 3)
    manifests = run_experiment(plan, backend, "t", say=lambda *a: None, session=2)
    assert len(manifests) == 3 and {m["repetition"] for m in manifests} == {2}
    assert sum(m["control"] for m in manifests) == 1
    assert len(backend.experiment) == 3


def test_dry_run_description_needs_no_cloud():
    from orchestrate.experiment import describe
    lines = []
    runs = describe({**PLAN, "control": {"configuration": "Base", "condition": "C1"}},
                    say=lines.append)
    assert len(runs) == 3
    # 3 runs of C1: 3 x (210 + 60) s = 810 s = 13.5 min -> "0 h 14 min"
    assert lines[0] == "t, whole plan: 3 runs, about 0 h 14 min"
    assert sum("A/A control" in line for line in lines) == 1
