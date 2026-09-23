"""The workspace preflight is shared by run_flow, the CSV runner and the GUI runner.

Each runner must check absolute targets before any device (or run folder) is
created, and stop with the same semantics: exit code 3 on the CLI,
``PreflightError`` for library callers. No hardware is opened here.
"""
import json
import os

import pytest

from src.devices.safety.validators import WorkspaceValidator
from src.flow import run_flow
from src.flow.csv_runner import run_csv
from src.gui import runner as gui_runner

EXAMPLE_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "flow", "csv_runner", "control.example.csv",
)

FAR_AWAY = {"action": "move_xyz", "robot_id": 1, "x": 100000.0, "y": 0.0, "z": 50.0}


def _forbidden(*a, **k):
    raise AssertionError("a device session / run folder was created despite a preflight violation")


# ----------------------------------------------------------------------
# run_flow: the reusable entry points
# ----------------------------------------------------------------------
def test_run_preflight_returns_report_and_logs(caplog):
    steps = [FAR_AWAY, {"action": "move_z", "robot_id": 1, "distance": -10.0}]
    with caplog.at_level("INFO", logger="run_flow"):
        report = run_flow.run_preflight(steps, WorkspaceValidator())
    assert not report.ok
    assert [i for i, _, _ in report.violations] == [1]
    assert [i for i, _ in report.unverified] == [2]
    assert "可動域違反" in caplog.text


def test_require_preflight_raises_with_step_and_coordinates():
    with pytest.raises(run_flow.PreflightError) as exc:
        run_flow.require_preflight([FAR_AWAY], WorkspaceValidator())
    assert "ステップ 1" in str(exc.value) and "100000" in str(exc.value)
    assert exc.value.report.violations


def test_require_preflight_passes_a_valid_flow():
    report = run_flow.require_preflight(
        [{"action": "move_xyz", "robot_id": 1, "x": 200.0, "y": 0.0, "z": 50.0}],
        WorkspaceValidator())
    assert report.ok and report.checked == 1


def test_run_flow_exit_code_is_the_shared_constant(tmp_path, monkeypatch):
    monkeypatch.setattr(run_flow, "ExperimentSession", _forbidden)
    monkeypatch.setattr(run_flow, "ExperimentLogger", _forbidden)
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"name": "t", "steps": [FAR_AWAY]}), encoding="utf-8")
    assert run_flow.main([str(path), "--mock"]) == run_flow.PREFLIGHT_EXIT_CODE == 3


# ----------------------------------------------------------------------
# CSV runner
# ----------------------------------------------------------------------
def _violating_preflight(steps, validator=None):
    report = run_flow.PreflightReport(checked=1)
    report.violations.append((1, dict(steps[0]), ValueError("x=100000 out of range")))
    return report


def test_csv_runner_runs_the_preflight_before_devices(monkeypatch, tmp_path):
    monkeypatch.setattr(run_csv, "ExperimentSession", _forbidden)
    monkeypatch.setattr(run_csv, "ExperimentLogger", _forbidden)
    monkeypatch.setattr(run_csv, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(run_flow, "preflight_workspace", _violating_preflight)
    assert run_csv.main(["--csv", EXAMPLE_CSV, "--mock"]) == 3
    assert not os.path.exists(tmp_path / "logs")


def test_csv_runner_validate_only_runs_the_preflight(monkeypatch):
    monkeypatch.setattr(run_flow, "preflight_workspace", _violating_preflight)
    assert run_csv.main(["--csv", EXAMPLE_CSV, "--validate-only"]) == 3


def test_csv_runner_preflight_reports_relative_moves_as_unverified(caplog):
    with caplog.at_level("INFO"):
        assert run_csv.main(["--csv", EXAMPLE_CSV, "--validate-only"]) == 0
    assert "unverified until run" in caplog.text


def test_csv_library_entry_raises_preflight_error(monkeypatch, tmp_path):
    """run_two_solution_mixing() checks too, for callers that skip main()."""
    import asyncio
    monkeypatch.setattr(run_csv, "ExperimentSession", _forbidden)
    monkeypatch.setattr(run_csv, "ExperimentLogger", _forbidden)
    monkeypatch.setattr(run_flow, "preflight_workspace", _violating_preflight)
    params = run_csv.load_params_from_csv(EXAMPLE_CSV)
    with pytest.raises(run_flow.PreflightError):
        asyncio.run(run_csv.run_two_solution_mixing(params, EXAMPLE_CSV, mock=True))


# ----------------------------------------------------------------------
# GUI runner
# ----------------------------------------------------------------------
def test_gui_runner_rejects_a_violating_flow_before_anything_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_runner, "ExperimentSession", _forbidden)
    monkeypatch.setattr(gui_runner, "ExperimentLogger", _forbidden)
    with pytest.raises(gui_runner.PreflightError):
        gui_runner.FlowRunner({"name": "t", "steps": [FAR_AWAY]}, mock=True,
                              logs_dir=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_gui_runner_keeps_the_preflight_report():
    runner = gui_runner.FlowRunner(
        {"name": "t", "steps": [{"action": "move_z", "robot_id": 1, "distance": -5.0}]},
        mock=True)
    assert runner.preflight.ok
    assert len(runner.preflight.unverified) == 1
