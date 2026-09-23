"""Pre-run safety checks: config loading, schema strictness, workspace preflight,
executor strictness and loop structure. No hardware is opened by any test here.
"""
import asyncio
import glob
import json
import math
import os
import sys

import pytest
from pydantic import ValidationError

from src import config as lab_config
from src.devices.safety.mock_robot import MockLabRobot
from src.devices.safety.validators import (
    WorkspaceValidator,
    WorkspaceViolationError,
    default_workspace_validator,
)
from src.flow import executor, run_flow
from src.flow.executor import execute_step, expand_loops
from src.flow.schema import ExperimentWorkflow

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_FLOWS = sorted(
    glob.glob(os.path.join(REPO_ROOT, "examples", "zif8", "*.json"))
    + glob.glob(os.path.join(REPO_ROOT, "examples", "microscope", "*.json"))
)


def write_flow(tmp_path, steps, name="flow.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"name": "t", "description": "d", "steps": steps}),
                    encoding="utf-8")
    return str(path)


@pytest.fixture
def restore_config():
    saved = (lab_config.CONFIG_PATH, lab_config.EXAMPLE_CONFIG_PATH, lab_config.MONITORING_DIR)
    yield
    lab_config.CONFIG_PATH, lab_config.EXAMPLE_CONFIG_PATH, lab_config.MONITORING_DIR = saved
    lab_config.load_config(force_reload=True)
    lab_config.load_monitoring_config(force_reload=True)


# ----------------------------------------------------------------------
# 1. config.yaml that exists but is unusable must raise, not fall back
# ----------------------------------------------------------------------
@pytest.mark.parametrize("text", ["workspace: [unclosed\n", "- a\n- b\n", "42\n"])
def test_broken_config_yaml_raises(tmp_path, restore_config, text):
    broken = tmp_path / "config.yaml"
    broken.write_text(text, encoding="utf-8")
    lab_config.CONFIG_PATH = str(broken)
    with pytest.raises(lab_config.ConfigError):
        lab_config.load_config(force_reload=True)


def test_broken_config_does_not_widen_validator(tmp_path, restore_config):
    """default_workspace_validator must not swallow the error and use +-300 mm."""
    broken = tmp_path / "config.yaml"
    broken.write_text("workspace: {x_min: -10\n", encoding="utf-8")
    lab_config.CONFIG_PATH = str(broken)
    lab_config._config_cache = None
    with pytest.raises(lab_config.ConfigError):
        default_workspace_validator()


def test_missing_pyyaml_raises_when_config_exists(tmp_path, restore_config, monkeypatch):
    own = tmp_path / "config.yaml"
    own.write_text("workspace:\n  z_min: -50\n", encoding="utf-8")
    lab_config.CONFIG_PATH = str(own)
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml -> ImportError
    with pytest.raises(lab_config.ConfigError, match="PyYAML"):
        lab_config.load_config(force_reload=True)


def test_absent_config_still_falls_back(tmp_path, restore_config):
    lab_config.CONFIG_PATH = str(tmp_path / "none.yaml")
    lab_config.EXAMPLE_CONFIG_PATH = str(tmp_path / "none.example.yaml")
    assert lab_config.load_config(force_reload=True) == lab_config.DEFAULTS


def test_broken_monitoring_config_raises(tmp_path, restore_config):
    (tmp_path / "config.yaml").write_text("video: [\n", encoding="utf-8")
    lab_config.MONITORING_DIR = str(tmp_path)
    with pytest.raises(lab_config.ConfigError):
        lab_config.load_monitoring_config(force_reload=True)


def test_run_flow_reports_config_error(tmp_path, restore_config, monkeypatch):
    broken = tmp_path / "config.yaml"
    broken.write_text("- x\n", encoding="utf-8")
    lab_config.CONFIG_PATH = str(broken)
    lab_config._config_cache = None
    flow = write_flow(tmp_path, [{"action": "move_xyz", "x": 200, "y": 0, "z": 50}])
    assert run_flow.main([flow, "--validate-only"]) == 2


# ----------------------------------------------------------------------
# 2. --validate-only / pre-run workspace preflight
# ----------------------------------------------------------------------
@pytest.mark.parametrize("flow", EXAMPLE_FLOWS, ids=os.path.basename)
def test_preflight_passes_every_example(flow):
    assert run_flow.main([flow, "--validate-only"]) == 0
    _, steps = run_flow.load_and_validate(flow)
    assert run_flow.preflight_workspace(steps).ok


def test_examples_exist():
    assert any("microscope" in f for f in EXAMPLE_FLOWS)
    assert any("zif8" in f for f in EXAMPLE_FLOWS)


def test_validate_only_rejects_far_move_xyz(tmp_path, caplog):
    flow = write_flow(tmp_path, [
        {"action": "go_home", "robot_id": 1},
        {"action": "move_xyz", "robot_id": 2, "x": 100000, "y": 0, "z": 50},
    ])
    with caplog.at_level("ERROR", logger="run_flow"):
        code = run_flow.main([flow, "--validate-only"])
    assert code != 0
    text = caplog.text
    assert "ステップ 2" in text and "Robot 2" in text
    assert "100000" in text          # offending coordinate
    assert "'x': {'min'" in text     # the configured limits are printed


def test_validate_only_rejects_absolute_rotate(tmp_path):
    flow = write_flow(tmp_path, [{"action": "rotate", "robot_id": 1, "angle": 179.0}])
    assert run_flow.main([flow, "--validate-only"]) != 0


def test_validate_only_rejects_nan_literal(tmp_path):
    path = tmp_path / "nan.json"
    path.write_text('{"name": "n", "steps": [{"action": "move_xyz", "x": NaN, "y": 0, "z": 0}]}',
                    encoding="utf-8")
    assert run_flow.main([str(path), "--validate-only"]) != 0


def test_preflight_itself_rejects_nan_without_schema():
    report = run_flow.preflight_workspace(
        [{"action": "move_xyz", "x": float("nan"), "y": 0.0, "z": 0.0},
         {"action": "rotate", "angle": float("inf")}],
        WorkspaceValidator())
    assert [i for i, _, _ in report.violations] == [1, 2]


def test_preflight_marks_relative_moves_unverified(caplog):
    steps = [{"action": "move_z", "robot_id": 1, "distance": -500.0},
             {"action": "rotate_relative", "robot_id": 1, "angle": 400.0, "speed": "low"},
             {"action": "move_radial", "robot_id": 1, "distance": 10.0},
             {"action": "move_xyz", "robot_id": 1, "x": 200.0, "y": 0.0, "z": 50.0}]
    report = run_flow.preflight_workspace(steps, WorkspaceValidator())
    assert report.ok and report.checked == 1
    assert [i for i, _ in report.unverified] == [1, 2, 3]
    with caplog.at_level("INFO", logger="run_flow"):
        run_flow.log_preflight(report, WorkspaceValidator())
    assert "unverified until run" in caplog.text


def test_preflight_blocks_real_run_before_any_device(tmp_path, monkeypatch):
    """Without --mock, a violating flow must stop before ExperimentSession exists."""
    def forbidden(*a, **k):
        raise AssertionError("a device session was created despite a preflight violation")
    monkeypatch.setattr(run_flow, "ExperimentSession", forbidden)
    monkeypatch.setattr(run_flow, "ExperimentLogger", forbidden)
    monkeypatch.setattr(run_flow, "ensure_dashboard_running", forbidden)
    flow = write_flow(tmp_path, [{"action": "move_xyz", "x": 100000, "y": 0, "z": 50}])
    assert run_flow.main([flow, "--no-record"]) == 3


# ----------------------------------------------------------------------
# 3. NaN / inf in schema and validator
# ----------------------------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_schema_rejects_non_finite(bad):
    with pytest.raises(ValidationError):
        ExperimentWorkflow(name="t", steps=[{"action": "move_xyz", "x": bad, "y": 0, "z": 0}])
    with pytest.raises(ValidationError):
        ExperimentWorkflow(name="t", steps=[{"action": "move_z", "distance": bad}])


def test_validator_rejects_non_finite_targets():
    v = WorkspaceValidator()
    nan = float("nan")
    for call in (lambda: v.validate_xyz(nan, 0, 0),
                 lambda: v.validate_xyz(0, 0, math.inf),
                 lambda: v.validate_joint1(nan),
                 lambda: v.validate_z_relative(0, nan),
                 lambda: v.validate_z_relative(nan, 1),
                 lambda: v.validate_joint1_relative(0, -math.inf)):
        with pytest.raises(WorkspaceViolationError) as exc:
            call()
        assert isinstance(exc.value, ValueError)
        str(exc.value)  # formats without error


def test_mock_robot_rejects_nan_move():
    robot = MockLabRobot(use_dobot=True, workspace_validator=WorkspaceValidator())
    with pytest.raises(WorkspaceViolationError):
        asyncio.run(robot.move_xyz(float("nan"), 0, 0))


@pytest.mark.parametrize("kwargs", [
    {"x_min": float("nan")}, {"z_max": float("inf")},
    {"x_min": 10, "x_max": -10}, {"joint1_min": 100, "joint1_max": 90},
])
def test_validator_rejects_bad_limits(kwargs):
    with pytest.raises(ValueError):
        WorkspaceValidator(**kwargs)


# ----------------------------------------------------------------------
# 4. unknown fields / unknown actions
# ----------------------------------------------------------------------
def test_misspelled_field_is_rejected():
    with pytest.raises(ValidationError, match="robotid"):
        ExperimentWorkflow(name="t", steps=[
            {"action": "move_xyz", "robotid": 2, "x": 200, "y": 0, "z": 50}])


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(ValidationError):
        ExperimentWorkflow(name="t", stpes=[], steps=[])


def test_iteration_key_is_added_after_validation():
    wf = ExperimentWorkflow(name="t", steps=[
        {"action": "loop_start", "loop_id": "a", "count": 2},
        {"action": "wait", "seconds": 0},
        {"action": "loop_end", "loop_id": "a"}])
    expanded = expand_loops([s.model_dump() for s in wf.steps])
    assert [s["_iteration"] for s in expanded] == [1, 2]


def test_gui_block_defaults_pass_strict_schema():
    """Every step the GUI palette can create must still validate (extra=forbid)."""
    import ast
    src = open(os.path.join(REPO_ROOT, "src", "gui", "app.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    config = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", None) == "ACTION_CONFIG" for t in n.targets))
    for action, cfg in config.items():
        if action in ("loop_start", "loop_end"):
            continue
        step = {"action": action, **cfg.get("defaults", {})}
        if action not in executor.SHARED_DEVICE_ACTIONS:
            step["robot_id"] = 1
        ExperimentWorkflow(name="t", steps=[step])


def test_executor_raises_on_unknown_robot_action():
    robot = MockLabRobot(use_dobot=True, workspace_validator=WorkspaceValidator())
    with pytest.raises(ValueError, match="self_destruct"):
        asyncio.run(execute_step({"action": "self_destruct", "robot_id": 1}, {1: robot}))


def test_executor_raises_on_unknown_shared_action(monkeypatch):
    monkeypatch.setattr(executor, "SHARED_DEVICE_ACTIONS",
                        executor.SHARED_DEVICE_ACTIONS | {"bogus"})
    with pytest.raises(ValueError, match="bogus"):
        asyncio.run(executor.execute_shared_device_step({"action": "bogus"}, object()))


# ----------------------------------------------------------------------
# 5. legacy execute_workflow cannot drive hardware
# ----------------------------------------------------------------------
def test_execute_workflow_is_disabled():
    with pytest.raises(NotImplementedError, match="run_flow"):
        asyncio.run(executor.execute_workflow("whatever.json"))


def test_executor_module_has_no_hardware_imports():
    assert not hasattr(executor, "LabRobot")
    assert not hasattr(executor, "SharedDevices")


# ----------------------------------------------------------------------
# 6. loop structure
# ----------------------------------------------------------------------
def test_mismatched_loop_end_inside_body_is_rejected():
    steps = [{"action": "loop_start", "loop_id": "a", "count": 2},
             {"action": "wait", "seconds": 0},
             {"action": "loop_end", "loop_id": "b"},
             {"action": "loop_end", "loop_id": "a"}]
    with pytest.raises(ValueError, match="id=b"):
        expand_loops(steps)


def test_mismatched_loop_end_rejected_by_validate_only(tmp_path):
    flow = write_flow(tmp_path, [
        {"action": "loop_start", "loop_id": "a", "count": 2},
        {"action": "loop_end", "loop_id": "b"},
        {"action": "loop_end", "loop_id": "a"}])
    assert run_flow.main([flow, "--validate-only"]) == 2
