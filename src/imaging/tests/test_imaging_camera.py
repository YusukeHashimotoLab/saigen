"""Items 2 and 3: verified camera writes, the run-owner lock on
/api/control, child-process environment, macOS uvc-util index (no hardware)."""
import io
import json

import pytest

import acquire
import camera_server
import capture
import hw
import paths


class FakeCamera:
    """Logical-name camera. `sticky=False` names accept a write but read
    back the old value; `failing` names report set() failure."""

    readable_names = hw.CONTROL_NAMES
    stale_supported = True

    def __init__(self, failing=(), sticky_off=()):
        self.values = {"auto_exposure": False, "exposure": 156, "gain": 0,
                       "auto_focus": False, "focus": 180, "auto_wb": False,
                       "wb_temp": 4600}
        self.failing, self.sticky_off = set(failing), set(sticky_off)
        self.stream_settings = dict(self.values)
        self.writes = []

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.writes.append((name, value))
        if name in self.failing:
            return False
        if name not in self.sticky_off:
            self.values[name] = value
        return True


@pytest.fixture
def cam(monkeypatch):
    c = FakeCamera()
    monkeypatch.setattr(camera_server, "camera", c)
    return c


PRESET = {"exposure": 20, "gain": 0, "focus": 180, "wb": 4600,
          "auto_exposure": False, "auto_focus": False}


def test_apply_camera_success_reads_back(cam):
    r = camera_server._apply_camera(PRESET)
    assert r["ok"] and r["diff"] == [] and r["failed_writes"] == []
    assert r["read_back"]["exposure"] == 20
    assert ("auto_wb", False) in cam.writes          # WB never re-enabled


def test_apply_camera_reports_failed_write(monkeypatch):
    c = FakeCamera(failing={"gain"})
    monkeypatch.setattr(camera_server, "camera", c)
    r = camera_server._apply_camera({**PRESET, "gain": 7})
    assert not r["ok"] and r["failed_writes"] == ["gain"]


def test_apply_camera_reports_read_back_diff(monkeypatch):
    c = FakeCamera(sticky_off={"exposure"})
    monkeypatch.setattr(camera_server, "camera", c)
    r = camera_server._apply_camera(PRESET)
    assert not r["ok"]
    assert r["diff"] == [{"key": "exposure", "requested": 20, "read_back": 156}]


def test_apply_camera_skips_unreadable_names(monkeypatch):
    c = FakeCamera(sticky_off={"auto_exposure"})
    c.readable_names = tuple(n for n in hw.CONTROL_NAMES if n != "auto_exposure")
    monkeypatch.setattr(camera_server, "camera", c)
    r = camera_server._apply_camera({**PRESET, "auto_exposure": True})
    assert r["ok"]


# ---------------------------------------------------------------- handler

class FakeHandler(camera_server.Handler):
    """Handler without a socket: collects _json() responses."""

    def __init__(self, headers=None):          # noqa: D401 - no super().__init__
        self.headers = headers or {}
        self.sent = []

    def _json(self, obj, status=200):
        self.sent.append((status, obj))


def _hold_run(monkeypatch, token="t0k"):
    monkeypatch.setattr(camera_server, "_measurement_idle", lambda: False)
    paths.RUN_OWNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.RUN_OWNER_PATH.write_text(token, encoding="utf-8")
    return token


def test_control_write_refused_during_run_without_token(cam, monkeypatch):
    _hold_run(monkeypatch)
    h = FakeHandler()
    h._set_control({"name": "exposure", "value": 30})
    assert h.sent[0][0] == 409
    assert cam.writes == []


def test_control_write_with_wrong_token_refused(cam, monkeypatch):
    _hold_run(monkeypatch)
    h = FakeHandler({paths.RUN_TOKEN_HEADER: "other"})
    h._set_control({"name": "whiteBalance", "value": 4600})
    assert h.sent[0][0] == 409 and cam.writes == []


def test_control_write_with_owner_token_allowed(cam, monkeypatch):
    tok = _hold_run(monkeypatch)
    h = FakeHandler({paths.RUN_TOKEN_HEADER: tok})
    h._set_control({"name": "whiteBalance", "value": 4600})
    assert h.sent[0] == (200, {"ok": True, "value": 4600})


def test_control_write_allowed_when_idle(cam, monkeypatch):
    monkeypatch.setattr(camera_server, "_measurement_idle", lambda: True)
    h = FakeHandler()
    h._set_control({"name": "autoFocus", "value": False})
    assert h.sent[0][0] == 200


def test_failed_bool_write_is_an_error(monkeypatch):
    monkeypatch.setattr(camera_server, "camera", FakeCamera(failing={"auto_focus"}))
    monkeypatch.setattr(camera_server, "_measurement_idle", lambda: True)
    h = FakeHandler()
    h._set_control({"name": "autoFocus", "value": False})
    assert h.sent[0] == (500, {"ok": False})


def _preset(tmp_path, monkeypatch, camera_doc):
    monkeypatch.setattr(camera_server, "CONDITIONS_DIR", tmp_path)
    monkeypatch.setattr(camera_server, "_measurement_idle", lambda: True)
    (tmp_path / "p1.json").write_text(json.dumps({"name": "p1", "camera": camera_doc}),
                                      encoding="utf-8")


def test_conditions_apply_stops_on_read_back_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(camera_server, "camera", FakeCamera(sticky_off={"focus"}))
    _preset(tmp_path, monkeypatch, {**PRESET, "focus": 100})
    h = FakeHandler()
    h._conditions_apply({"name": "p1"})
    status, body = h.sent[0]
    assert status == 500 and body["diff"][0]["key"] == "focus"


def test_conditions_apply_flags_restart_when_stream_stale(cam, tmp_path, monkeypatch):
    _preset(tmp_path, monkeypatch, PRESET)          # exposure 156 -> 20
    h = FakeHandler()
    h._conditions_apply({"name": "p1"})
    status, body = h.sent[0]
    assert status == 200 and body["restart_required"] is True
    assert "Restart" in body["note"]
    assert "reflected in measurement values" not in json.dumps(body)


def test_conditions_apply_no_note_when_not_stale(cam, tmp_path, monkeypatch):
    _preset(tmp_path, monkeypatch, {"exposure": 156})
    h = FakeHandler()
    h._conditions_apply({"name": "p1"})
    status, body = h.sent[0]
    assert status == 200 and body["restart_required"] is False
    assert "note" not in body


# ------------------------------------------------ acquisition side

def test_fix_white_balance_returns_read_back(monkeypatch):
    vals = {"autoWhiteBalance": True, "whiteBalance": 5000}

    def set_control(n, v):
        vals[n] = v
        return v
    monkeypatch.setattr(capture, "get_control", lambda n: vals[n])
    monkeypatch.setattr(capture, "set_control", set_control)
    monkeypatch.setattr(acquire.time, "sleep", lambda s: None)
    assert acquire._fix_white_balance() == {"autoWhiteBalance": False,
                                            "whiteBalance": 4600}


def test_fix_white_balance_raises_when_write_fails(monkeypatch):
    def set_control(n, v):
        raise RuntimeError("Camera control failed")
    monkeypatch.setattr(capture, "get_control", lambda n: None)
    monkeypatch.setattr(capture, "set_control", set_control)
    with pytest.raises(RuntimeError, match="Camera control failed"):
        acquire._fix_white_balance()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_capture_client_sends_owner_token_and_uses_server_path(monkeypatch):
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req)
        if req.full_url.endswith("/api/capture"):
            return _Resp(json.dumps({"ok": True, "file": "a.jpg",
                                     "path": "/abs/a.jpg"}).encode())
        return _Resp(json.dumps({"ok": True, "value": 4600}).encode())
    monkeypatch.setattr(capture.urllib.request, "urlopen", urlopen)
    capture.set_run_token("abc")
    try:
        assert capture.set_control("whiteBalance", 4600) == 4600
        assert str(capture.capture_photo()).replace("\\", "/").endswith("/abs/a.jpg")
    finally:
        capture.set_run_token(None)
    assert seen[0].get_header(paths.RUN_TOKEN_HEADER.capitalize()) == "abc"


def test_capture_client_raises_on_409(monkeypatch):
    import urllib.error

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {},
                                     io.BytesIO(b'{"ok": false, "error": "locked"}'))
    monkeypatch.setattr(capture.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="locked"):
        capture.set_control("exposure", 20)


# ------------------------------------------------ child env / port / data dir

def test_child_env_carries_port_and_resolved_data_dir(monkeypatch):
    monkeypatch.setattr(camera_server, "server_url", "http://127.0.0.1:9123")
    env = camera_server._child_env()
    assert env["IMAGING_SERVER_URL"] == "http://127.0.0.1:9123"
    assert env["IMAGING_DATA_DIR"] == str(paths.DATA_DIR)
    assert paths.DATA_DIR.is_absolute()
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_server_url_from_env(monkeypatch):
    monkeypatch.setenv("IMAGING_SERVER_URL", "http://127.0.0.1:9000/")
    assert paths.server_url() == "http://127.0.0.1:9000"
    monkeypatch.delenv("IMAGING_SERVER_URL")
    assert paths.server_url() == paths.DEFAULT_SERVER_URL


def test_env_setting_reads_dotenv_without_exporting(tmp_path, monkeypatch):
    monkeypatch.delenv("NEEWER_DEVICE_ID", raising=False)
    f = tmp_path / ".env"
    f.write_text("# c\nNEEWER_DEVICE_ID=abc-123\n", encoding="utf-8")
    assert paths.env_setting("NEEWER_DEVICE_ID", f) == "abc-123"
    import os
    assert "NEEWER_DEVICE_ID" not in os.environ
    monkeypatch.setenv("NEEWER_DEVICE_ID", "env-wins")
    assert paths.env_setting("NEEWER_DEVICE_ID", f) == "env-wins"


# ------------------------------------------------ macOS uvc-util index

UVC_LIST = """\
------------ -------------- ------------ ------------ ------------------------------------------------
Index        Vend:Prod      LocationID   UVC Version  Device name
------------ -------------- ------------ ------------ ------------------------------------------------
           0 0x05ac:0x8514   0x81000000       1.00    FaceTime HD Camera
           1 0x046d:0x082d   0x14200000       1.00    HD Pro Webcam C920
------------ -------------- ------------ ------------ ------------------------------------------------
"""


def test_uvc_index_resolved_by_name():
    devs = hw.parse_uvc_device_list(UVC_LIST)
    assert devs == [{"index": 0, "name": "FaceTime HD Camera"},
                    {"index": 1, "name": "HD Pro Webcam C920"}]
    assert hw.resolve_uvc_index(devs, "HD Pro Webcam C920") == 1


def test_uvc_index_missing_or_ambiguous_fails():
    devs = hw.parse_uvc_device_list(UVC_LIST)
    with pytest.raises(RuntimeError, match="not found"):
        hw.resolve_uvc_index(devs[:1], "HD Pro Webcam C920")
    two = devs + [{"index": 2, "name": "HD Pro Webcam C920"}]
    with pytest.raises(RuntimeError, match="several"):
        hw.resolve_uvc_index(two, "HD Pro Webcam C920")


def test_mac_camera_uses_resolved_index(monkeypatch):
    import subprocess
    calls = []

    class R:
        returncode, stdout, stderr = 0, "180\n", ""

    def run(argv, **kw):
        calls.append(argv)
        r = R()
        if "-d" in argv:
            r.stdout = UVC_LIST
        return r
    monkeypatch.setattr(subprocess, "run", run)
    cam = hw._MacCamera.__new__(hw._MacCamera)      # no ffmpeg thread
    cam.uvc_index = cam._resolve_uvc_index()
    assert cam.get("focus") == 180
    assert calls[-1][1:3] == ["-I", "1"]
    assert cam.device_info()["index"] == 1
