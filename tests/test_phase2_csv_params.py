"""CSV runner: unknown / duplicate parameter rows are errors with line numbers;
comment rows and the documented optional rows keep working."""
import csv

import pytest

from src.flow.csv_runner import run_csv
from src.flow.csv_runner.run_csv import PARAM_SPECS, load_params_from_csv

BASE = [
    ("robot1_volume_mL", "2.0"),
    ("robot2_volume_mL", "3.0"),
    ("aspirate_z_descent_mm", "50.0"),
    ("dispense_z_descent_mm", "40.0"),
    ("robot1_dispense_speed", "5"),
    ("robot2_dispense_speed", "7"),
    ("robot1_angle_deg", "90.0"),
    ("robot2_angle_deg", "-90.0"),
]


def write_rows(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["parameter", "value", "note"])
        for row in rows:
            w.writerow(list(row) + [""] * (3 - len(row)))
    return str(path)


def test_optional_rows_may_be_omitted(tmp_path):
    params = load_params_from_csv(write_rows(tmp_path / "c.csv", BASE))
    assert params["aspirate_speed"] == 1
    assert params["capture_photo"] is False
    assert params["blow_out_after_dispense"] is False
    assert params["robot1_dispense_radial_mm"] == 0.0
    assert set(params) == set(PARAM_SPECS)


def test_comment_and_blank_rows_are_ignored(tmp_path):
    rows = [("# provenance", "", "anything"), ("", "", "")] + BASE + [("#note", "x")]
    load_params_from_csv(write_rows(tmp_path / "c.csv", rows))


def test_unknown_parameter_is_rejected_with_its_line(tmp_path):
    rows = BASE + [("robot1_volume_ml", "9.0")]         # typo: lower-case ml
    with pytest.raises(ValueError) as exc:
        load_params_from_csv(write_rows(tmp_path / "c.csv", rows))
    msg = str(exc.value)
    assert f"line {len(rows) + 1}" in msg                # header is line 1
    assert "unknown parameter 'robot1_volume_ml'" in msg


def test_duplicate_parameter_is_rejected_with_both_lines(tmp_path):
    rows = BASE + [("robot2_dispense_speed", "1")]
    with pytest.raises(ValueError) as exc:
        load_params_from_csv(write_rows(tmp_path / "c.csv", rows))
    msg = str(exc.value)
    first = 1 + 1 + [k for k, _ in BASE].index("robot2_dispense_speed")
    assert f"line {len(rows) + 1}: 'robot2_dispense_speed' is given twice" in msg
    assert f"first on line {first}" in msg


def test_all_problems_are_reported_together(tmp_path):
    rows = BASE + [("bogus", "1"), ("robot1_angle_deg", "0"), ("aspirate_speed", "99")]
    with pytest.raises(ValueError) as exc:
        load_params_from_csv(write_rows(tmp_path / "c.csv", rows))
    msg = str(exc.value)
    assert "bogus" in msg and "given twice" in msg and "aspirate_speed" in msg


def test_main_returns_2_for_an_unknown_row(tmp_path, monkeypatch):
    monkeypatch.setattr(run_csv, "ExperimentSession",
                        lambda *a, **k: pytest.fail("no session for an invalid sheet"))
    path = write_rows(tmp_path / "c.csv", BASE + [("robot3_volume_mL", "1")])
    assert run_csv.main(["--csv", path, "--validate-only"]) == 2


@pytest.mark.parametrize("volume", ["0.1", "0.49"])
def test_volume_below_the_pipette_minimum_is_rejected(tmp_path, volume):
    rows = [(k, volume if k == "robot1_volume_mL" else v) for k, v in BASE]
    with pytest.raises(ValueError) as exc:
        load_params_from_csv(write_rows(tmp_path / "c.csv", rows))
    assert "pipette minimum" in str(exc.value)


@pytest.mark.parametrize("volume", ["0", "0.5", "10"])
def test_zero_and_valid_volumes_are_accepted(tmp_path, volume):
    rows = [(k, volume if k == "robot1_volume_mL" else v) for k, v in BASE]
    load_params_from_csv(write_rows(tmp_path / "c.csv", rows))
