#!/usr/bin/env python3
"""Thin HTTP client for the running camera server (camera_server.py).

Provides the capture and control primitives that acquire.py builds on:
triggering a single photo, reading/writing camera control values, and
reading the server's /api/health. Camera control always goes through this
HTTP API (not the device directly) because on Windows, DirectShow holds the
camera exclusively and only the server process may open it; the same code
path is used on macOS.

The server address comes from IMAGING_SERVER_URL (see paths.server_url);
camera_server.py sets it for the acquisition child it launches, so a server
started with --port is found. While an acquisition holds the measurement
lock it registers an owner token with set_run_token(); every control write
then carries that token, and the server refuses writes without it.
"""
import json
import urllib.error
import urllib.request
from pathlib import Path

import paths

SERVER = paths.server_url()   # resolved once at import (the child's env is fixed)

_run_token: str | None = None


def set_run_token(token: str | None) -> None:
    """Attach (or with None, detach) the run's owner token to control writes."""
    global _run_token
    _run_token = token


def _headers(extra: dict | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    if _run_token:
        h[paths.RUN_TOKEN_HEADER] = _run_token
    if extra:
        h.update(extra)
    return h


def _post(path: str, payload: dict | None, timeout: float) -> dict:
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f"{SERVER}{path}", data=body, method="POST",
                                 headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # the server answers errors with a JSON body; surface it
        try:
            return {"ok": False, "http_status": e.code, **json.loads(e.read())}
        except (ValueError, OSError):
            return {"ok": False, "http_status": e.code}


def capture_photo() -> Path:
    """Ask the running server to take a photo and return the saved file's path.

    The server reports the absolute path it wrote; that is used when
    present, so a hand-run acquire.py with a different IMAGING_DATA_DIR
    still finds the file.
    """
    res = _post("/api/capture", None, timeout=15)
    if not res.get("ok"):
        raise RuntimeError(f"Capture failed: {res}")
    if res.get("path"):
        return Path(res["path"])
    return paths.PHOTOS_DIR / res["file"]


def get_controls() -> dict:
    with urllib.request.urlopen(f"{SERVER}/api/controls", timeout=10) as r:
        return json.loads(r.read())


def get_control(name: str):
    """Read a control value from the server (= the process that owns the camera).

    name is one of the control names exposed by camera_server.py
    (exposure/gain/focus/autoExposure/autoFocus/whiteBalance/autoWhiteBalance).
    """
    v = get_controls().get(name)
    return v.get("value") if isinstance(v, dict) else v


def set_control(name: str, value):
    """Write a control value via the server. Raises RuntimeError on failure.

    Returns the value the server actually wrote (sliders are clamped).
    """
    res = _post("/api/control", {"name": name, "value": value}, timeout=10)
    if not res.get("ok"):
        raise RuntimeError(f"Camera control failed ({name}={value}): {res}")
    return res.get("value", value)


def get_health() -> dict:
    """Return the server's /api/health document (raises on connection errors)."""
    try:
        with urllib.request.urlopen(f"{SERVER}/api/health", timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"/api/health failed: HTTP {e.code}") from e
