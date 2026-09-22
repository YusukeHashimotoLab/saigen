"""csv_runner: CSV parameter validation, generated step list, results.csv, mock run"""
import csv
import os

import pytest

from src.flow.csv_runner import run_csv
from src.flow.csv_runner.run_csv import (
    PARAM_SPECS,
    build_steps,
    build_workflow,
    load_params_from_csv,
    write_results_csv,
)
from src.flow.schema import ExperimentWorkflow

VALID_PARAMS = {
    "robot1_volume_mL": "2.0",
    "robot2_volume_mL": "3.0",
    "aspirate_z_descent_mm": "50.0",
    "dispense_z_descent_mm": "40.0",
    "aspirate_speed": "1",
    "robot1_dispense_speed": "5",
    "robot2_dispense_speed": "7",
    "robot1_angle_deg": "90.0",
    "robot2_angle_deg": "-90.0",
    "capture_photo": "FALSE",
}

EXAMPLE_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "flow", "csv_runner", "control.example.csv",
)


def write_control_csv(path, values):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["parameter", "value", "note"])
        for k, v in values.items():
            writer.writerow([k, v, ""])
    return str(path)


# ----------------------------------------------------------------------
# CSV parameter validation
# ----------------------------------------------------------------------
def test_load_valid_params(tmp_path):
    path = write_control_csv(tmp_path / "control.csv", VALID_PARAMS)
    params = load_params_from_csv(path)
    assert params["robot1_volume_mL"] == 2.0
    assert params["robot1_dispense_speed"] == 5
    assert isinstance(params["robot1_dispense_speed"], int)
    assert set(params.keys()) == set(PARAM_SPECS.keys())


def test_missing_param_raises(tmp_path):
    values = dict(VALID_PARAMS)
    del values["robot2_volume_mL"]
    path = write_control_csv(tmp_path / "control.csv", values)
    with pytest.raises(ValueError, match="robot2_volume_mL"):
        load_params_from_csv(path)


def test_out_of_range_raises(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             dict(VALID_PARAMS, robot1_volume_mL="11.0"))
    with pytest.raises(ValueError, match="out of range"):
        load_params_from_csv(path)


def test_type_error_raises(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             dict(VALID_PARAMS, robot1_dispense_speed="fast"))
    with pytest.raises(ValueError, match="robot1_dispense_speed"):
        load_params_from_csv(path)


def test_missing_columns_raise(tmp_path):
    path = tmp_path / "control.csv"
    path.write_text("name,setting\nrobot1_volume_mL,2.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="parameter"):
        load_params_from_csv(str(path))


# ----------------------------------------------------------------------
# aspirate_speed: a CSV parameter, not a hidden constant
# ----------------------------------------------------------------------
def test_aspirate_speed_read_from_csv(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             dict(VALID_PARAMS, aspirate_speed="3"))
    params = load_params_from_csv(path)
    assert params["aspirate_speed"] == 3
    aspirations = [s for s in build_steps(params)[0] if s["action"] == "aspirate"]
    assert aspirations and all(s["speed"] == 3 for s in aspirations)


def test_aspirate_speed_defaults_to_1_when_absent(tmp_path):
    """The lab runner aspirated at the hard-coded speed 1 and had no CSV row for
    it, so a sheet without the row must reproduce that historical behaviour."""
    values = dict(VALID_PARAMS)
    del values["aspirate_speed"]
    path = write_control_csv(tmp_path / "control.csv", values)
    params = load_params_from_csv(path)
    assert params["aspirate_speed"] == 1
    assert run_csv.ASPIRATE_SPEED_DEFAULT == 1
    aspirations = [s for s in build_steps(params)[0] if s["action"] == "aspirate"]
    assert aspirations and all(s["speed"] == 1 for s in aspirations)


def test_aspirate_speed_out_of_range_raises(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             dict(VALID_PARAMS, aspirate_speed="10"))
    with pytest.raises(ValueError, match="aspirate_speed"):
        load_params_from_csv(path)


# ----------------------------------------------------------------------
# The published example CSV is valid and covers every parameter
# ----------------------------------------------------------------------
def test_example_csv_loads_and_lists_every_parameter():
    with open(EXAMPLE_CSV, encoding="utf-8-sig", newline="") as f:
        rows = {r["parameter"]: r for r in csv.DictReader(f)
                if not r["parameter"].startswith("#")}
    assert set(rows) == set(PARAM_SPECS)
    assert all(rows[name]["note"] for name in rows), "every row needs a note"
    params = load_params_from_csv(EXAMPLE_CSV)
    ExperimentWorkflow(**build_workflow(params)[0].model_dump())


# ----------------------------------------------------------------------
# Generated step list
# ----------------------------------------------------------------------
def test_generated_steps_validate_against_schema(tmp_path):
    path = write_control_csv(tmp_path / "control.csv", VALID_PARAMS)
    params = load_params_from_csv(path)
    steps, weight_owner = build_steps(params)

    # validates through the same Pydantic model the JSON flows use
    workflow = ExperimentWorkflow(name="t", description="t", steps=steps)
    assert [s.model_dump()["action"] for s in workflow.steps] == \
           [s["action"] for s in steps]

    actions = [s["action"] for s in steps]
    assert actions.count("aspirate") == 2
    assert actions.count("dispense") == 2
    assert actions.count("measure_weight") == 2
    assert actions.count("tare_scale") == 2
    # every dispense is preceded by a tare and followed (later) by a weighing
    assert actions.index("tare_scale") < actions.index("dispense")
    assert set(weight_owner.values()) == {1, 2}
    # per-robot values land on the right steps
    d1, d2 = [s for s in steps if s["action"] == "dispense"]
    assert (d1["robot_id"], d1["volume"], d1["speed"]) == (1, 2.0, 5)
    assert (d2["robot_id"], d2["volume"], d2["speed"]) == (2, 3.0, 7)


def test_step_order_matches_the_lab_runner(tmp_path):
    """Pin the exact ten-step sequence per robot.

    This is the sequence of the lab runner (commit 4584333) that produced the
    paper's ZIF-8 batches: the balance is tared with the tip already lowered into
    the vial, the mass is read before the tip rises, and the arm is returned by
    `go_home` rather than by a second `rotate_relative`.
    """
    path = write_control_csv(tmp_path / "control.csv", VALID_PARAMS)
    params = load_params_from_csv(path)
    steps, weight_owner = build_steps(params)

    expected = [
        "move_z", "aspirate", "move_z", "rotate_relative", "move_z",
        "tare_scale", "dispense", "measure_weight", "move_z", "go_home",
    ]
    assert [s["action"] for s in steps] == expected * 2
    # Robot 1 runs its whole sequence before Robot 2 starts
    assert [s["robot_id"] for s in steps if "robot_id" in s] == [1] * 8 + [2] * 8

    r1 = steps[:10]
    assert r1[0]["distance"] == -params["aspirate_z_descent_mm"]
    assert r1[1]["speed"] == params["aspirate_speed"]
    assert r1[2]["distance"] == params["aspirate_z_descent_mm"]
    assert r1[3]["angle"] == params["robot1_angle_deg"]
    assert r1[4]["distance"] == -params["dispense_z_descent_mm"]
    assert r1[5]["delay"] == run_csv.TARE_DELAY_S
    assert r1[6]["speed"] == params["robot1_dispense_speed"]
    assert r1[7]["stabilization_count"] == run_csv.DEFAULT_STABILIZATION_COUNT
    assert r1[8]["distance"] == params["dispense_z_descent_mm"]

    # no rotation back and no photograph in the historical sequence
    assert [s["angle"] for s in steps if s["action"] == "rotate_relative"] == \
           [params["robot1_angle_deg"], params["robot2_angle_deg"]]
    assert not any(s["action"] == "capture_and_save" for s in steps)

    # the weighing indices point at the measure_weight steps (1-based)
    assert weight_owner == {8: 1, 18: 2}
    for index, rid in weight_owner.items():
        assert steps[index - 1]["action"] == "measure_weight"


def test_capture_photo_defaults_to_false_and_can_be_enabled(tmp_path):
    values = dict(VALID_PARAMS)
    del values["capture_photo"]
    path = write_control_csv(tmp_path / "control.csv", values)
    params = load_params_from_csv(path)
    assert params["capture_photo"] is False
    assert not any(s["action"] == "capture_and_save" for s in build_steps(params)[0])

    path = write_control_csv(tmp_path / "on.csv",
                             dict(VALID_PARAMS, capture_photo="TRUE"))
    params = load_params_from_csv(path)
    assert params["capture_photo"] is True
    steps, weight_owner = build_steps(params)
    assert steps[-1] == {"action": "capture_and_save", "file_path": ""}
    assert [s["action"] for s in steps].count("capture_and_save") == 1
    # the photograph must not shift the weighing indices
    assert weight_owner == {8: 1, 18: 2}
    ExperimentWorkflow(name="t", description="t", steps=steps)


def test_capture_photo_rejects_a_non_boolean(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             dict(VALID_PARAMS, capture_photo="maybe"))
    with pytest.raises(ValueError, match="capture_photo"):
        load_params_from_csv(path)


def test_zero_volume_skips_that_robot(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             dict(VALID_PARAMS, robot1_volume_mL="0"))
    steps, weight_owner = build_steps(load_params_from_csv(path))
    assert {s.get("robot_id") for s in steps if "robot_id" in s} == {2}
    assert set(weight_owner.values()) == {2}
    ExperimentWorkflow(name="t", description="t", steps=steps)


def test_both_volumes_zero_gives_no_steps(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             dict(VALID_PARAMS, robot1_volume_mL="0",
                                  robot2_volume_mL="0"))
    steps, _ = build_steps(load_params_from_csv(path))
    assert steps == []


# ----------------------------------------------------------------------
# results.csv written back for the spreadsheet user
# ----------------------------------------------------------------------
def test_results_csv_contains_dispense_speeds(tmp_path):
    """Regression: the results file must name parameters that actually exist
    (an earlier version wrote an always-empty `pipette_speed` column)."""
    input_csv = write_control_csv(tmp_path / "control.csv", VALID_PARAMS)
    params = load_params_from_csv(input_csv)
    results = {"robot1_weight_g": 2.01, "robot2_weight_g": 2.98}

    out_path = write_results_csv(input_csv, params, results)

    with open(out_path, encoding="utf-8-sig", newline="") as f:
        rows = {r["key"]: r["value"] for r in csv.DictReader(f)}
    assert rows["robot1_dispense_speed"] == "5"
    assert rows["robot2_dispense_speed"] == "7"
    assert rows["aspirate_speed"] == "1"
    assert "pipette_speed" not in rows
    assert rows["robot1_weight_g"] == "2.01"


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _latest_run_dir(logs_dir):
    runs = [os.path.join(r, d) for r, ds, _ in os.walk(logs_dir) for d in ds
            if os.path.exists(os.path.join(r, d, "metadata.json"))]
    assert runs, f"no run folder was created under {logs_dir}"
    return max(runs, key=os.path.getmtime)


def test_bad_value_is_rejected_before_any_device_is_created(tmp_path, monkeypatch):
    """A typo in the spreadsheet must never reach the hardware."""
    def fail(*a, **kw):  # pragma: no cover - must not be called
        raise AssertionError("a session was created despite an invalid CSV")

    monkeypatch.setattr(run_csv, "ExperimentSession", fail)
    monkeypatch.setattr(run_csv, "ExperimentLogger", fail)
    monkeypatch.setattr(run_csv, "LOGS_DIR", str(tmp_path / "logs"))
    path = write_control_csv(tmp_path / "control.csv",
                             dict(VALID_PARAMS, dispense_z_descent_mm="900"))

    assert run_csv.main(["--csv", path, "--mock"]) == 2
    assert not os.path.exists(tmp_path / "logs")


def test_missing_csv_returns_2(tmp_path):
    assert run_csv.main(["--csv", str(tmp_path / "nope.csv"), "--mock"]) == 2


def test_validate_only_builds_steps_without_running(tmp_path, monkeypatch):
    monkeypatch.setattr(run_csv, "ExperimentSession", lambda *a, **kw: pytest.fail(
        "no session may be created with --validate-only"))
    path = write_control_csv(tmp_path / "control.csv", VALID_PARAMS)
    assert run_csv.main(["--csv", path, "--validate-only"]) == 0


def test_default_csv_path_prefers_control_csv(monkeypatch, tmp_path):
    control = tmp_path / "control.csv"
    monkeypatch.setattr(run_csv, "CONTROL_CSV_PATH", str(control))
    monkeypatch.setattr(run_csv, "EXAMPLE_CSV_PATH", EXAMPLE_CSV)
    assert run_csv.default_csv_path() == EXAMPLE_CSV
    control.write_text("parameter,value,note\n", encoding="utf-8")
    assert run_csv.default_csv_path() == str(control)


# ----------------------------------------------------------------------
# Mock run: same artefacts as a JSON run
# ----------------------------------------------------------------------
def test_mock_run_writes_run_folder_with_two_weights(tmp_path, monkeypatch):
    monkeypatch.setattr(run_csv, "LOGS_DIR", str(tmp_path / "logs"))
    path = write_control_csv(tmp_path / "control.csv", VALID_PARAMS)

    assert run_csv.main(["--csv", path, "--mock"]) == 0

    run_dir = _latest_run_dir(str(tmp_path / "logs"))
    for name in ("run.log", "measurements.csv", "summary.md", "metadata.json",
                 "generated_flow.json", "control.csv"):
        assert os.path.exists(os.path.join(run_dir, name)), name

    with open(os.path.join(run_dir, "measurements.csv"),
              encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    weights = [r for r in rows if r["action"] == "measure_weight" and r["weight_g"]]
    assert len(weights) == 2, f"expected two weighings, got {len(weights)}"

    # the same weights reach results.csv next to the input CSV
    with open(tmp_path / "results.csv", encoding="utf-8-sig", newline="") as f:
        results = {r["key"]: r["value"] for r in csv.DictReader(f)}
    assert results["robot1_weight_g"] and results["robot2_weight_g"]
    assert results["log_dir"] == run_dir


def test_mock_run_records_dispense_accuracy(tmp_path, monkeypatch):
    monkeypatch.setattr(run_csv, "LOGS_DIR", str(tmp_path / "logs"))
    path = write_control_csv(tmp_path / "control.csv", VALID_PARAMS)
    assert run_csv.main(["--csv", path, "--mock", "--liquid-density", "0.789"]) == 0

    accuracy = os.path.join(_latest_run_dir(str(tmp_path / "logs")),
                            "dispense_accuracy.csv")
    assert os.path.exists(accuracy)
    with open(accuracy, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["density_g_per_mL"] == "0.7890"


def test_workspace_violation_from_csv_fails_the_run(tmp_path, monkeypatch):
    """A CSV value inside the parameter range but outside the cell limits is caught
    by the same workspace validator that guards the JSON flows (joint 1 is limited
    to +/-135 deg by config.yaml, while the CSV allows +/-360)."""
    monkeypatch.setattr(run_csv, "LOGS_DIR", str(tmp_path / "logs"))
    path = write_control_csv(tmp_path / "control.csv",
                             dict(VALID_PARAMS, robot1_angle_deg="200.0"))

    assert run_csv.main(["--csv", path, "--mock"]) == 1
    meta_path = os.path.join(_latest_run_dir(str(tmp_path / "logs")), "metadata.json")
    import json
    meta = json.load(open(meta_path, encoding="utf-8"))
    assert meta["status"] == "failed"


# ----------------------------------------------------------------------
# Dispense-position offset and blow-out (the lab runner's later additions)
# ----------------------------------------------------------------------
LAB_EXTRAS = {
    "robot1_dispense_radial_mm": "-15",
    "robot2_dispense_radial_mm": "-18",
    "blow_out_after_dispense": "1",
}


def test_extras_absent_reproduce_the_historical_sequence(tmp_path):
    """A sheet without the new rows generates exactly the old ten steps per robot."""
    path = write_control_csv(tmp_path / "control.csv", VALID_PARAMS)
    params = load_params_from_csv(path)
    assert params["robot1_dispense_radial_mm"] == 0.0
    assert params["robot2_dispense_radial_mm"] == 0.0
    assert params["blow_out_after_dispense"] is False

    steps, weight_owner = build_steps(params)
    assert len(steps) == 20
    assert not any(s["action"] in ("move_radial", "blow_out") for s in steps)
    assert weight_owner == {8: 1, 18: 2}


def test_radial_offset_inserts_move_radial_after_the_rotation(tmp_path):
    path = write_control_csv(tmp_path / "control.csv", {**VALID_PARAMS, **LAB_EXTRAS,
                                                         "blow_out_after_dispense": "0"})
    steps, weight_owner = build_steps(load_params_from_csv(path))

    r1 = steps[:11]
    assert [s["action"] for s in r1] == [
        "move_z", "aspirate", "move_z", "rotate_relative", "move_radial", "move_z",
        "tare_scale", "dispense", "measure_weight", "move_z", "go_home",
    ]
    assert r1[4] == {"action": "move_radial", "robot_id": 1, "distance": -15.0}
    r2 = steps[11:]
    assert r2[4] == {"action": "move_radial", "robot_id": 2, "distance": -18.0}
    # weighing indices follow the inserted steps (1-based)
    assert weight_owner == {9: 1, 20: 2}
    for index, rid in weight_owner.items():
        assert steps[index - 1]["action"] == "measure_weight"


def test_zero_radial_offset_for_one_robot_only(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             {**VALID_PARAMS, "robot2_dispense_radial_mm": "12.5"})
    steps, weight_owner = build_steps(load_params_from_csv(path))
    radials = [s for s in steps if s["action"] == "move_radial"]
    assert radials == [{"action": "move_radial", "robot_id": 2, "distance": 12.5}]
    assert weight_owner == {8: 1, 19: 2}


def test_blow_out_is_inserted_between_dispense_and_weighing(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             {**VALID_PARAMS, "blow_out_after_dispense": "TRUE"})
    params = load_params_from_csv(path)
    steps, weight_owner = build_steps(params)

    actions = [s["action"] for s in steps]
    assert actions[:11] == [
        "move_z", "aspirate", "move_z", "rotate_relative", "move_z",
        "tare_scale", "dispense", "blow_out", "measure_weight", "move_z", "go_home",
    ]
    b1, b2 = [s for s in steps if s["action"] == "blow_out"]
    assert b1 == {"action": "blow_out", "robot_id": 1, "go_home": True,
                  "speed": params["robot1_dispense_speed"], "delay_ms": run_csv.BLOW_OUT_DELAY_MS}
    assert b2["robot_id"] == 2 and b2["speed"] == params["robot2_dispense_speed"]
    assert run_csv.BLOW_OUT_DELAY_MS == 3000
    assert weight_owner == {9: 1, 20: 2}


def test_lab_sheet_with_both_extras_validates_against_schema(tmp_path):
    path = write_control_csv(tmp_path / "control.csv", {**VALID_PARAMS, **LAB_EXTRAS})
    params = load_params_from_csv(path)
    workflow, weight_owner = build_workflow(params)
    actions = [s.action for s in workflow.steps]
    assert actions.count("move_radial") == 2
    assert actions.count("blow_out") == 2
    assert len(actions) == 24
    assert weight_owner == {10: 1, 22: 2}


def test_radial_offset_out_of_range_raises(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             {**VALID_PARAMS, "robot1_dispense_radial_mm": "150"})
    with pytest.raises(ValueError):
        load_params_from_csv(path)


def test_blow_out_rejects_a_non_boolean(tmp_path):
    path = write_control_csv(tmp_path / "control.csv",
                             {**VALID_PARAMS, "blow_out_after_dispense": "maybe"})
    with pytest.raises(ValueError):
        load_params_from_csv(path)


def test_results_csv_contains_the_extras(tmp_path):
    input_csv = write_control_csv(tmp_path / "control.csv", {**VALID_PARAMS, **LAB_EXTRAS})
    params = load_params_from_csv(input_csv)
    out_path = write_results_csv(input_csv, params, {"robot1_weight_g": 5.0, "robot2_weight_g": 4.9})
    with open(out_path, encoding="utf-8-sig", newline="") as f:
        rows = {r["key"]: r["value"] for r in csv.DictReader(f)}
    assert rows["robot1_dispense_radial_mm"] == "-15.0"
    assert rows["robot2_dispense_radial_mm"] == "-18.0"
    assert rows["blow_out_after_dispense"] == "True"


def test_mock_run_with_the_lab_extras_completes(tmp_path, monkeypatch):
    """End to end in mock mode with the offset and blow-out enabled: the extra
    steps run through MockLabRobot and the two weighings still land in results."""
    monkeypatch.setattr(run_csv, "LOGS_DIR", str(tmp_path / "logs"))
    path = write_control_csv(tmp_path / "control.csv", {**VALID_PARAMS, **LAB_EXTRAS})

    assert run_csv.main(["--csv", path, "--mock"]) == 0

    run_dir = _latest_run_dir(str(tmp_path / "logs"))
    with open(os.path.join(run_dir, "measurements.csv"),
              encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    actions = [r["action"] for r in rows]
    assert actions.count("move_radial") == 2
    assert actions.count("blow_out") == 2
    weights = [r for r in rows if r["action"] == "measure_weight" and r["weight_g"]]
    assert len(weights) == 2

    with open(tmp_path / "results.csv", encoding="utf-8-sig", newline="") as f:
        results = {r["key"]: r["value"] for r in csv.DictReader(f)}
    assert results["robot1_weight_g"] and results["robot2_weight_g"]
