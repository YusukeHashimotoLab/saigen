"""analyze_dispense_log: statistics per target volume; n<2 is undefined."""
import csv

import pytest

from src.gui import analyze_dispense_log as analyze

FIELDS = ["target_volume_mL", "density_g_per_mL", "expected_weight_g", "measured_weight_g"]


def write_accuracy(run, rows):
    run.mkdir(exist_ok=True)
    with open(run / analyze.ACCURACY_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for vol, weight in rows:
            w.writerow({"target_volume_mL": f"{vol:.4f}", "density_g_per_mL": "1.0000",
                        "expected_weight_g": f"{vol:.4f}", "measured_weight_g": f"{weight:.4f}"})
    return run


def test_groups_by_target_volume(tmp_path):
    run = write_accuracy(tmp_path / "run", [(1, 0.99), (5, 4.98), (1, 1.01), (5, 5.02)])
    groups, _ = analyze.load_groups(str(run))
    assert [(g["volume"], g["values"]) for g in groups] == [(1.0, [0.99, 1.01]),
                                                           (5.0, [4.98, 5.02])]
    assert [g["nominal"] for g in groups] == [pytest.approx(1.0), pytest.approx(5.0)]


def test_single_measurement_statistics_are_undefined():
    s = analyze.summarize([5.0], nominal=5.0)
    assert s["stdev"] is None and s["variance"] is None and s["cv"] is None
    text = analyze.format_report([5.0], "src", s)
    assert analyze.UNDEFINED in text
    assert "0.0000 g   (sample" not in text


def test_cli_prints_one_report_per_volume(tmp_path, capsys):
    run = write_accuracy(tmp_path / "run", [(1, 0.99), (1, 1.01), (5, 4.9)])
    assert analyze.main([str(run)]) == 0
    out = capsys.readouterr().out
    assert out.count("Dispensing accuracy report") == 2
    assert "target volume: 1 mL" in out and "target volume: 5 mL" in out
    # the 5 mL group has n = 1 -> undefined, not 0.0
    five = out.split("target volume: 5 mL")[1]
    assert analyze.UNDEFINED in five
    # CV is not pooled across volumes: the 1 mL group's CV is ~1.4 %
    one = out.split("target volume: 1 mL")[1].split("target volume: 5 mL")[0]
    assert "1.41 %" in one


def test_measurements_csv_forms_one_group(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    with open(run / analyze.MEASUREMENTS_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["action", "status", "weight_g"])
        w.writeheader()
        w.writerow({"action": "measure_weight", "status": "ok", "weight_g": "4.9"})
        w.writerow({"action": "measure_weight", "status": "error", "weight_g": "0.1"})
    groups, source = analyze.load_groups(str(run))
    assert source.endswith(analyze.MEASUREMENTS_CSV)
    assert groups == [{"volume": None, "nominal": None, "values": [4.9]}]
