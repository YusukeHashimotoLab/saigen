"""Sensor ingestion (TCP :50001) and the CSV recording loop of the dashboard.

No port is bound: connections are simulated with ``socket.socketpair()`` and
the handler is driven directly, exactly as the accept loop would call it.
"""
import csv
import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard"))

import launch_sensor_dashboard as dash  # noqa: E402

GOOD = {
    "type": "sensor_data", "temp": 24.5, "humi": 41.2,
    "accel": [0.0, 0.01, 0.99], "gyro": [0.1, -0.2, 0.0],
    "lux_raw": 812, "uv": 0.12, "voc": 30211,
    "ok": {"bme280": True, "tsl25911": True, "icm20948": True, "ltr390": True, "sgp40": True},
    "model": "BME280 (T/H)", "hostname": "pi-a",
}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(dash, "latest_sensor_data", {})
    monkeypatch.setattr(dash, "asyncio_loop", None)   # broadcasts become no-ops
    monkeypatch.setattr(dash, "PI_TOKEN", None)
    monkeypatch.setattr(dash, "PI_ALLOWED_NETS", [])


# --- validation --------------------------------------------------------------

def test_valid_message_is_kept_and_unknown_fields_dropped():
    clean = dash.validate_sensor_message(dict(GOOD, evil="<script>", ip="1.2.3.4"))
    assert clean["temp"] == 24.5 and clean["accel"] == [0.0, 0.01, 0.99]
    assert "evil" not in clean and "ip" not in clean
    assert clean["ok"]["sgp40"] is True


def test_nulls_are_accepted_everywhere():
    msg = dict(GOOD, temp=None, humi=None, lux_raw=None, uv=None, voc=None, accel=None,
               gyro=[None, 1.0, None], ok={"bme280": False})
    clean = dash.validate_sensor_message(msg)
    assert clean["temp"] is None and clean["accel"] is None
    assert clean["gyro"] == [None, 1.0, None]
    assert clean["ok"] == {"bme280": False, "tsl25911": False, "icm20948": False,
                           "ltr390": False, "sgp40": False}


def test_legacy_lux_key_maps_to_lux_raw_and_ok_is_inferred():
    legacy = {k: v for k, v in GOOD.items() if k not in ("lux_raw", "ok")}
    legacy["lux"] = 640
    clean = dash.validate_sensor_message(legacy)
    assert clean["lux_raw"] == 640 and "lux" not in clean
    assert clean["ok"]["tsl25911"] is True


@pytest.mark.parametrize("patch", [
    {"accel": [1.0, 2.0]},               # short list
    {"accel": "1,2,3"},                  # not a list
    {"gyro": [1, 2, "x"]},               # non-number element
    {"temp": "25"},                      # string reading
    {"voc": True},                       # bool is not a number
    {"humi": float("nan")},
    {"ok": ["bme280"]},
    {"ok": {"bme280": "yes"}},
])
def test_malformed_messages_are_rejected(patch):
    with pytest.raises(dash.InvalidSensorMessage):
        dash.validate_sensor_message(dict(GOOD, **patch))


def test_non_object_is_rejected():
    with pytest.raises(dash.InvalidSensorMessage):
        dash.validate_sensor_message([1, 2, 3])


# --- the connection handler --------------------------------------------------

def _run_client(lines, addr=("192.0.2.21", 40000)):
    """Feed raw lines through handle_pi_client; return what was stored while connected."""
    server, client = socket.socketpair()
    seen = {}
    real_broadcast = dash._broadcast_threadsafe

    def capture(message):
        if message.get("type") == "sensor_data":
            seen.setdefault("samples", []).append(message)
        seen.setdefault("types", []).append(message.get("type"))
        real_broadcast(message)

    dash._broadcast_threadsafe = capture
    try:
        t = threading.Thread(target=dash.handle_pi_client, args=(server, addr))
        t.start()
        for line in lines:
            client.sendall(line if isinstance(line, bytes) else (line + "\n").encode())
        client.close()
        t.join(timeout=5)
        assert not t.is_alive()
    finally:
        dash._broadcast_threadsafe = real_broadcast
    return seen


def test_malformed_lines_are_dropped_and_the_connection_survives(caplog):
    lines = [
        json.dumps(dict(GOOD, accel=None, temp=None)),      # nulls: fine
        json.dumps({"type": "sensor_data", "accel": None}),  # the review's crash case
        json.dumps(dict(GOOD, accel=[1.0])),                  # short list: dropped
        "not json at all",
        json.dumps(dict(GOOD, temp=30.0)),                    # still ingested afterwards
    ]
    with caplog.at_level("WARNING"):
        seen = _run_client(lines)
    temps = [s["temp"] for s in seen["samples"]]
    assert temps == [None, None, 30.0]
    assert any("malformed" in r.message for r in caplog.records)
    assert seen["types"][0] == "connect" and seen["types"][-1] == "disconnect"
    last = seen["samples"][-1]
    assert last["ip"] == "192.0.2.21" and "received_at" in last


def test_token_required_when_configured(monkeypatch):
    monkeypatch.setattr(dash, "PI_TOKEN", "pi-secret")
    seen = _run_client([json.dumps(GOOD)])
    assert "samples" not in seen and "types" not in seen   # never even announced


def test_wrong_token_is_refused(monkeypatch):
    monkeypatch.setattr(dash, "PI_TOKEN", "pi-secret")
    seen = _run_client([json.dumps({"type": "auth", "token": "nope"}), json.dumps(GOOD)])
    assert "samples" not in seen


def test_correct_token_admits_the_agent(monkeypatch):
    monkeypatch.setattr(dash, "PI_TOKEN", "pi-secret")
    # auth line and first sample in one packet, as a fast agent may send them
    payload = (json.dumps({"type": "auth", "token": "pi-secret"}) + "\n" + json.dumps(GOOD) + "\n").encode()
    seen = _run_client([payload])
    assert [s["voc"] for s in seen["samples"]] == [30211]


@pytest.mark.parametrize("ip,allowed", [
    ("192.0.2.21", True), ("192.0.2.5", True), ("198.51.100.7", False),
    ("::ffff:192.0.2.21", True), ("garbage", False),
])
def test_pi_allow_list(ip, allowed):
    nets = dash._parse_ip_allowlist("192.0.2.21, 192.0.2.0/28")
    assert dash._pi_address_allowed(ip, nets) is allowed


def test_empty_allow_list_accepts_everyone():
    assert dash._pi_address_allowed("203.0.113.9", []) is True


def test_bad_allow_list_entry_raises():
    with pytest.raises(dash.DashboardConfigError):
        dash._parse_ip_allowlist("192.0.2.1,not-an-ip")


# --- recording loop ----------------------------------------------------------

def test_row_records_tick_and_arrival_time_and_blank_nulls(tmp_path):
    msg = dash.validate_sensor_message(dict(GOOD, temp=None, accel=None))
    msg.update(ip="192.0.2.21", hostname="pi-a", received_at="2026-09-23T00:00:00+00:00")
    row = dash._sensor_row("192.0.2.21", msg, "2026-09-23T00:00:01+00:00")
    assert row["timestamp"] == "2026-09-23T00:00:01+00:00"
    assert row["received_at"] == "2026-09-23T00:00:00+00:00"
    assert row["lux_raw"] == 812 and row["temperature"] is None and row["acc_z"] is None

    rec = dash.CsvRecorder(str(tmp_path / "s.csv"), dash.CSV_FIELDS)
    rec.write_row(row)
    rec.close()
    with open(tmp_path / "s.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["temperature"] == "" and rows[0]["lux_raw"] == "812"
    assert "lux" not in rows[0]


def test_recording_loop_survives_a_bad_entry(monkeypatch):
    written = []

    class FakeRecorder:
        def write_row(self, row):
            written.append(row["hostname"])

    good = dict(dash.validate_sensor_message(GOOD), hostname="good", received_at="t")
    monkeypatch.setattr(dash, "latest_sensor_data", {
        "192.0.2.1": {"accel": 5, "hostname": "bad"},   # not a list: _sensor_row raises
        "192.0.2.2": good,
    })
    monkeypatch.setitem(dash.recording_state, "sensor_recorder", FakeRecorder())
    monkeypatch.setitem(dash.recording_state, "is_recording", True)
    t = threading.Thread(target=dash.recording_loop)
    t.start()
    time.sleep(0.35)
    dash.recording_state["is_recording"] = False
    t.join(timeout=2)
    assert not t.is_alive()
    assert written.count("good") >= 2 and "bad" not in written
