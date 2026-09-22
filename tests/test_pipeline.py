"""
Minimal smoke + invariant tests for people_picking_pipeline.py.

Run:
    python -m pytest tests/ -v
or, with no pytest installed:
    python tests/test_pipeline.py
"""
import os
import shutil
import subprocess
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE = os.path.join(ROOT, "people_picking_pipeline.py")
EXAMPLE_RAW = os.path.join(ROOT, "examples", "example_raw_qualtrics.csv")
EXAMPLE_ROSTER = os.path.join(ROOT, "examples", "example_roster.csv")


def _run(outdir, extra_args=None):
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    cmd = [sys.executable, PIPELINE, "--in", EXAMPLE_RAW, "--outdir", outdir, "--seed", "1"]
    cmd += extra_args or []
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"pipeline failed:\n{result.stdout}\n{result.stderr}"
    return result


def test_runs_respondent_only(tmp_path=None):
    outdir = os.path.join(ROOT, "tests", "_tmp_out_resp_only")
    _run(outdir, ["--no-viz"])
    scored = pd.read_csv(os.path.join(outdir, "pp_pick_popularity_all_scored_nq_v.csv"))
    # every respondent's nQ scores should average close to 100 (mean-100 SD-15 rescaling)
    for col in ["nQ(O)", "nQ(I)", "nQ(C)"]:
        assert abs(scored[col].mean() - 100) < 5, f"{col} mean drifted: {scored[col].mean()}"
    # no duplicate x/y merge-collision columns
    assert not any(c.endswith("_x") or c.endswith("_y") for c in scored.columns)
    shutil.rmtree(outdir)


def test_runs_full_cohort_roster():
    outdir = os.path.join(ROOT, "tests", "_tmp_out_full_cohort")
    _run(outdir, ["--roster", EXAMPLE_ROSTER])
    roster = pd.read_csv(os.path.join(outdir, "roster_full_cohort.csv"))
    assert roster["is_respondent"].sum() < len(roster), "expected some non-respondents in the roster"
    diagrams = os.listdir(os.path.join(outdir, "network_diagrams"))
    assert len(diagrams) == 5
    shutil.rmtree(outdir)


def test_preview_and_unfinished_rows_dropped_by_default():
    outdir = os.path.join(ROOT, "tests", "_tmp_out_filter_check")
    result = _run(outdir, ["--no-viz"])
    assert "dropped preview/blank: 1" in result.stdout or "dropped preview/blank:1" in result.stdout
    assert "dropped unfinished: 1" in result.stdout or "dropped unfinished:1" in result.stdout
    shutil.rmtree(outdir)


if __name__ == "__main__":
    test_runs_respondent_only()
    test_runs_full_cohort_roster()
    test_preview_and_unfinished_rows_dropped_by_default()
    print("All smoke tests passed.")
