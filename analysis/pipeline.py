"""All figures and tables of an experiment in one step (M7), run after every experiment.

For each experiment folder (results/<experiment_id>, with experiment.json):
  1. metrics of every valid run (metrics.csv, windows.csv, buckets.csv per run,
     and the experiment's metrics.csv);
  2. the figure of every valid run (figure.png and figure.pdf in the run's folder);
  3. the experiment's own summary (summary.csv, summary.tex, comparison.png,
     slo_matrix.png, ...).
Then, for every plan with more than one experiment (the five sessions of the full
plan, say), a combined summary over all of them in results/<plan>-summary/.

The orchestrator calls this at the end of every experiment, so the figures are
there as soon as a session ends. It can also be run at any time without AWS:
  .venv/bin/python analysis/pipeline.py full        every experiment of the plan "full"
  .venv/bin/python analysis/pipeline.py --all       every experiment in results/
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.metrics import analyse_experiment  # noqa: E402
from analysis.plot_run import plot_run  # noqa: E402
from analysis.summarize import summarise_experiment, summarise_experiments  # noqa: E402

RESULTS = ROOT / "results"


def experiments(results_dir, plan_name=None):
    """Experiment folders in results_dir, oldest first, optionally only one plan's."""
    found = []
    for folder in sorted(Path(results_dir).iterdir()):
        record_path = folder / "experiment.json"
        if not record_path.is_file():
            continue
        record = json.loads(record_path.read_text())
        if plan_name is None or record.get("plan", {}).get("name") == plan_name:
            found.append((folder, record.get("plan", {}).get("name")))
    return found


def process_experiment(folder, say=print):
    """Metrics, run figures and the summary of one experiment; returns the number of runs."""
    folder = Path(folder)
    rows = analyse_experiment(folder)
    drawn = 0
    for row in rows:
        try:                      # one figure that cannot be drawn must not stop the rest
            plot_run(folder.parent / row["run_id"])
            drawn += 1
        except Exception as err:                                # noqa: BLE001
            say(f"  figure of {row['run_id']} failed: {err!r}")
    if rows:
        summarise_experiment(folder)
    say(f"  {folder.name}: {len(rows)} runs -> metrics, {drawn} run figures, summary")
    return len(rows)


def process(results_dir=RESULTS, plan_name=None, say=print, quick=False):
    """Process the experiments (of one plan, or all), then each plan's combined summary.

    quick: keep each run's metrics and figure as they are and only rebuild the
    summaries and outputs from the existing metrics.csv files (seconds, not minutes).
    Returns the folders of the combined summaries written.
    """
    found = experiments(results_dir, plan_name)
    if not found:
        say(f"no experiment folders{' for plan ' + plan_name if plan_name else ''} "
            f"in {results_dir}")
        return []
    say(f"{len(found)} experiment(s):")
    by_plan = {}
    for folder, name in found:
        if quick:
            if (folder / "metrics.csv").exists():
                summarise_experiment(folder)
                by_plan.setdefault(name, []).append(folder)
                say(f"  {folder.name}: summary rebuilt from its metrics.csv")
            else:
                say(f"  {folder.name}: no metrics.csv yet; run without --quick")
        elif process_experiment(folder, say):
            by_plan.setdefault(name, []).append(folder)
    combined = []
    for name, folders in by_plan.items():
        if len(folders) > 1:
            out = Path(results_dir) / f"{name}-summary"
            summarise_experiments(folders, out, say=say)
            say(f"  combined summary of {len(folders)} {name} experiments -> {out}")
            combined.append(out)
    return combined


def after_experiment(experiment_id, plan_name, results_dir=RESULTS, say=print):
    """What the orchestrator runs when an experiment ends. Never raises: the runs are
    already saved in S3, and a failed figure must not look like a failed session."""
    try:
        say("analysis:")
        process_experiment(Path(results_dir) / experiment_id, say)
        folders = [folder for folder, _ in experiments(results_dir, plan_name)
                   if (folder / "metrics.csv").exists()]
        if len(folders) > 1:
            out = Path(results_dir) / f"{plan_name}-summary"
            summarise_experiments(folders, out, say=say)
            say(f"  combined summary of {len(folders)} {plan_name} experiments -> {out}")
        return True
    except Exception as err:                                    # noqa: BLE001
        say(f"  analysis failed ({err!r}); the runs are saved. Repeat it with "
            f"make results PLAN={plan_name}")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", nargs="?", help="plan name, e.g. full, pilot or shakedown")
    parser.add_argument("--all", action="store_true", help="every experiment in results/")
    parser.add_argument("--quick", action="store_true",
                        help="only rebuild summaries and outputs from existing metrics")
    args = parser.parse_args()
    if not args.plan and not args.all:
        parser.error("give a plan name (e.g. full) or --all")
    process(RESULTS, None if args.all else args.plan, quick=args.quick)
    return 0


if __name__ == "__main__":
    sys.exit(main())
