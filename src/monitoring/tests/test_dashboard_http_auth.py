"""Token and same-origin protection of the dashboard's HTTP endpoints.

The FastAPI app is driven with TestClient without entering its context
manager, so the lifespan (TCP listener, zeroconf, browser launch) never runs.
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard"))

import launch_sensor_dashboard as dash  # noqa: E402

TOKEN = "s3cret-token"
SAME_ORIGIN = "http://localhost:8000"


@pytest.fixture
def client():
    return TestClient(dash.app, base_url="http://localhost:8000")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setitem(dash.recording_state, "save_dir", str(tmp_path))
    monkeypatch.setitem(dash.recording_state, "is_recording", False)
    calls = []

    def fake_begin():
        calls.append("begin")
        dash.recording_state["is_recording"] = True
        dash.recording_state["session_timestamp"] = "20260923_000000"

    monkeypatch.setattr(dash, "begin_recording_session", fake_begin)
    monkeypatch.setattr(dash, "finalize_recording_session", lambda: None)
    yield calls
    dash.recording_state["is_recording"] = False


def _post(client, path, headers=None):
    if path == "/api/upload_video":
        return client.post(path, headers=headers or {},
                           files={"video": ("v.webm", b"\x1a\x45\xdf\xa3", "video/webm")},
                           data={"timestamp": "20260923_000000", "prefix": ""})
    if path == "/api/detect_tags":
        return client.post(path, headers=headers or {}, files={"image": ("f.jpg", b"", "image/jpeg")})
    if path == "/api/start":
        return client.post(path, headers=headers or {}, json={})
    return client.post(path, headers=headers or {})


NEW_TOKEN_ENDPOINTS = ["/api/upload_video", "/api/detect_tags", "/api/zero_tags", "/api/clear_zero"]
ALL_STATE_CHANGING = ["/api/start", "/api/end"] + NEW_TOKEN_ENDPOINTS


@pytest.mark.parametrize("path", NEW_TOKEN_ENDPOINTS)
def test_token_required_when_configured(client, monkeypatch, path):
    monkeypatch.setattr(dash, "AUTH_TOKEN", TOKEN)
    assert _post(client, path).status_code == 401
    assert _post(client, path, {"X-Auth-Token": "wrong"}).status_code == 401
    assert _post(client, path, {"X-Auth-Token": TOKEN}).status_code == 200


@pytest.mark.parametrize("path", NEW_TOKEN_ENDPOINTS)
def test_no_token_configured_keeps_local_dashboard_usable(client, monkeypatch, path):
    monkeypatch.setattr(dash, "AUTH_TOKEN", None)
    assert _post(client, path, {"Origin": SAME_ORIGIN}).status_code == 200


@pytest.mark.parametrize("path", ALL_STATE_CHANGING)
@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://localhost:9999"},            # other port = other origin
    {"Referer": "http://evil.example/page.html"},  # no Origin, foreign Referer
])
def test_cross_origin_requests_are_refused(client, monkeypatch, path, headers, _isolate):
    monkeypatch.setattr(dash, "AUTH_TOKEN", None)
    if path == "/api/end":
        dash.recording_state["is_recording"] = True
    r = _post(client, path, headers)
    assert r.status_code == 403
    assert _isolate == []   # nothing was started


def test_cross_site_post_cannot_stop_a_recording(client, monkeypatch):
    """The review's case: /api/end has no body, so a plain HTML form could hit it."""
    monkeypatch.setattr(dash, "AUTH_TOKEN", None)
    dash.recording_state["is_recording"] = True
    r = client.post("/api/end", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert dash.recording_state["is_recording"] is True


def test_cross_origin_refused_even_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(dash, "AUTH_TOKEN", TOKEN)
    r = client.post("/api/zero_tags", headers={"Origin": "http://evil.example", "X-Auth-Token": TOKEN})
    assert r.status_code == 403


def test_same_origin_and_non_browser_clients_pass(client, monkeypatch, _isolate):
    monkeypatch.setattr(dash, "AUTH_TOKEN", None)
    assert client.post("/api/start", json={}, headers={"Origin": SAME_ORIGIN}).status_code == 200
    assert client.post("/api/end", headers={"Referer": SAME_ORIGIN + "/"}).status_code == 200
    # flow runner / GUI: no Origin, no Referer
    assert client.post("/api/start", json={}).status_code == 200
    assert _isolate == ["begin", "begin"]


def test_upload_video_writes_only_with_token(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "AUTH_TOKEN", TOKEN)
    _post(client, "/api/upload_video")
    assert not any(p.suffix == ".webm" for p in tmp_path.iterdir())
    assert _post(client, "/api/upload_video", {"X-Auth-Token": TOKEN}).status_code == 200
    assert any(p.suffix == ".webm" for p in tmp_path.iterdir())


@pytest.mark.parametrize("source,host,expected", [
    ("http://localhost:8000", "localhost:8000", True),
    ("http://LOCALHOST:8000/x?y", "localhost:8000", True),
    ("https://localhost:8000", "localhost:8000", True),
    ("http://localhost:8001", "localhost:8000", False),
    ("http://evil.example", "localhost:8000", False),
    ("null", "localhost:8000", False),
    ("file:///c:/x.html", "localhost:8000", False),
    (None, "localhost:8000", False),
])
def test_same_origin_helper(source, host, expected):
    assert dash._same_origin(source, host) is expected
