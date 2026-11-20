"""The automatic analysis after each experiment: metrics, run figures, summaries."""
import json

from analysis.pipeline import after_experiment, experiments, process
from test_ct6_metrics import T0, fixture, write_gz


def make_experiment(results, experiment_id, plan_name, configurations=("Base",)):
    """An experiment folder with one valid C1 run per configuration (CT6 fixture data)."""
    run_ids = []
    for k, configuration in enumerate(configurations):
        run_id = f"{experiment_id}-{k + 1:03d}-C1-{configuration}-r1-a1"
        run = results / run_id
        run.mkdir(parents=True)
        records, logs = fixture()
        write_gz(run / "records.jsonl.gz", records)
        write_gz(run / "lambda_logs.jsonl.gz", logs)
        write_gz(run / "versions.jsonl.gz", [])
        (run / "manifest.json").write_text(json.dumps({
            "run_id": run_id, "experiment": experiment_id, "configuration": configuration,
            "condition": "C1", "repetition": 1, "attempt": 1, "valid": True,
            "faults": {"RUN_START": T0 // 1000}, "run_start": T0 // 1000}))
        run_ids.append(run_id)
    folder = results / experiment_id
    folder.mkdir()
    (folder / "experiment.json").write_text(json.dumps(
        {"experiment": experiment_id, "plan": {"name": plan_name}, "valid_runs": run_ids}))
    return folder, run_ids


def test_one_experiment_gets_metrics_run_figures_and_summary(tmp_path):
    folder, run_ids = make_experiment(tmp_path, "full-s1-1Z", "full", ("Base", "Cache"))
    lines = []
    assert process(tmp_path, "full", say=lines.append) == []      # one session: no combination
    for run_id in run_ids:
        assert (tmp_path / run_id / "figure.png").exists()
        assert (tmp_path / run_id / "metrics.csv").exists()
    for name in ("metrics.csv", "summary.csv", "comparison.png", "slo_matrix.png"):
        assert (folder / name).exists(), name
    assert any("2 runs" in line for line in lines)


def test_sessions_of_a_plan_are_combined_and_other_plans_kept_apart(tmp_path):
    make_experiment(tmp_path, "full-s1-1Z", "full")
    make_experiment(tmp_path, "full-s2-2Z", "full")
    make_experiment(tmp_path, "pilot-3Z", "pilot")
    assert [name for _, name in experiments(tmp_path)] == ["full", "full", "pilot"]
    assert [f.name for f, _ in experiments(tmp_path, "pilot")] == ["pilot-3Z"]
    combined = process(tmp_path, None, say=lambda *a: None)
    assert [c.name for c in combined] == ["full-summary"]          # pilot has only one
    summary = (tmp_path / "full-summary" / "summary.csv").read_text().splitlines()
    assert summary[1].startswith("C1,Base,2,")                     # two runs, one per session


def test_after_experiment_updates_the_combined_summary(tmp_path):
    make_experiment(tmp_path, "full-s1-1Z", "full")
    lines = []
    assert after_experiment("full-s1-1Z", "full", tmp_path, say=lines.append) is True
    assert not (tmp_path / "full-summary").exists()               # nothing to combine yet
    make_experiment(tmp_path, "full-s2-2Z", "full")
    assert after_experiment("full-s2-2Z", "full", tmp_path, say=lines.append) is True
    assert (tmp_path / "full-summary" / "slo_matrix.png").exists()
    assert any("combined summary of 2 full experiments" in line for line in lines)


def test_after_experiment_never_raises(tmp_path):
    lines = []
    assert after_experiment("missing-1Z", "full", tmp_path, say=lines.append) is False
    assert "make results PLAN=full" in lines[-1]


def test_no_experiments_is_reported(tmp_path):
    lines = []
    assert process(tmp_path, "full", say=lines.append) == []
    assert "no experiment folders for plan full" in lines[0]


def test_one_failing_figure_does_not_stop_the_others(tmp_path, monkeypatch):
    import analysis.pipeline as pipeline
    folder, run_ids = make_experiment(tmp_path, "full-s1-1Z", "full", ("Base", "Cache"))
    real = pipeline.plot_run

    def flaky(run_dir):
        if run_dir.name == run_ids[0]:
            raise ValueError("cannot draw")
        return real(run_dir)
    monkeypatch.setattr(pipeline, "plot_run", flaky)
    lines = []
    assert pipeline.process_experiment(folder, say=lines.append) == 2
    assert (tmp_path / run_ids[1] / "figure.png").exists()
    assert any("failed" in line for line in lines) and "1 run figures" in lines[-1]


def test_a_breaker_experiment_without_base_is_analysed(tmp_path):
    # The Day 27 plan has only the four breaker configurations, so nothing to
    # compare against Base inside it; every figure and table must still appear.
    breaker = ("Breaker", "Retry+Breaker", "Cache+Breaker", "Retry+Cache+Breaker")
    folder, run_ids = make_experiment(tmp_path, "breaker-1Z", "breaker", breaker)
    lines = []
    process(tmp_path, "breaker", say=lines.append)
    for run_id in run_ids:
        assert (tmp_path / run_id / "figure.png").exists()
    for name in ("summary.csv", "comparison.png", "slo_matrix.png"):
        assert (folder / name).exists(), name
    assert any("4 runs" in line for line in lines)
