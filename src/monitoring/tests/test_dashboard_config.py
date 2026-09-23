"""The dashboard's config.yaml loader must refuse a broken file, not fall back."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard"))

import launch_sensor_dashboard as dash  # noqa: E402


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_valid_file_overrides_defaults(tmp_path):
    p = _write(tmp_path, "config.yaml", "sensor_dashboard:\n  web_port: 8100\n")
    assert dash._load_dashboard_ports([p]) == {"web_port": 8100, "tcp_port": 50001}


def test_missing_files_use_defaults(tmp_path):
    assert dash._load_dashboard_ports([str(tmp_path / "nope.yaml")]) == {"web_port": 8000, "tcp_port": 50001}


def test_empty_file_uses_defaults(tmp_path):
    assert dash._load_dashboard_ports([_write(tmp_path, "c.yaml", "")])["tcp_port"] == 50001


def test_first_existing_file_wins_even_if_broken(tmp_path):
    broken = _write(tmp_path, "config.yaml", "sensor_dashboard: [unclosed\n")
    good = _write(tmp_path, "config.example.yaml", "sensor_dashboard:\n  web_port: 8000\n")
    with pytest.raises(dash.DashboardConfigError, match="Could not read"):
        dash._load_dashboard_ports([broken, good])


@pytest.mark.parametrize("text,match", [
    ("- a\n- b\n", "top level must be a mapping"),
    ("sensor_dashboard: 8000\n", "must be a mapping"),
    ("sensor_dashboard:\n  tcp_port: fifty\n", "port number"),
    ("sensor_dashboard:\n  tcp_port: 70000\n", "port number"),
    ("sensor_dashboard:\n  web_port: true\n", "port number"),
])
def test_unusable_content_raises(tmp_path, text, match):
    with pytest.raises(dash.DashboardConfigError, match=match):
        dash._load_dashboard_ports([_write(tmp_path, "config.yaml", text)])


def test_shipped_example_is_valid():
    example = os.path.join(dash.MONITORING_DIR, "config.example.yaml")
    assert dash._load_dashboard_ports([example]) == {"web_port": 8000, "tcp_port": 50001}
