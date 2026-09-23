#!/usr/bin/env python3
"""Camera control server for spectral-imaging capture rigs.

Serves a live preview plus exposure/gain/focus/white-balance control over a
local web app. Frame acquisition and camera control are delegated to a
platform-abstraction module (hw.py):

  - macOS  : ffmpeg (AVFoundation) + uvc-util
  - Windows: OpenCV (DirectShow)

DirectShow holds the camera exclusively, so on Windows this server is the
only process allowed to open it. Other processes (e.g. the spectral
acquisition script) must go through this server's HTTP API
(/api/control, /api/controls) for camera control.

Run:  python camera_server.py [--host HOST] [--port PORT]
URL:  http://127.0.0.1:8799 (defaults; see --help)
"""
import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import hw
import paths

STATIC_DIR = paths.STATIC_DIR
CAMERA_REFERENCE = paths.CAMERA_REFERENCE
PORT = 8799

# Slider-style controls: public API name -> (hw logical name, min, max).
# Ranges are platform-specific (on Windows/DirectShow exposure is on a log2
# seconds scale and negative; the values below follow the measured values
# from commit 48a7233).
if hw.IS_WIN:
    SLIDERS = {
        "exposure": ("exposure", -13, 0),
        "gain": ("gain", 0, 255),
        "focus": ("focus", 0, 255),
    }
else:
    SLIDERS = {
        "exposure": ("exposure", 3, 2047),   # uvc-util: exposure-time-abs
        "gain": ("gain", 0, 255),
        "focus": ("focus", 0, 250),
    }
# Boolean controls: public API name -> hw logical name (True = auto).
BOOLS = {"autoExposure": "auto_exposure", "autoFocus": "auto_focus",
         "autoWhiteBalance": "auto_wb"}
WB_MIN, WB_MAX = 2000, 6500   # white balance color temperature (Kelvin)

# Created in main(), not here: hw.CameraSource() opens the video device
# immediately (starts the ffmpeg/DirectShow capture process), so importing
# this module must not construct it.
camera: "hw.CameraSource | None" = None

# The base URL the acquisition child must call back on. Set in main() from
# the actual --host/--port (a wildcard bind is reached via 127.0.0.1).
server_url: str = paths.DEFAULT_SERVER_URL


def _child_env() -> dict:
    """Environment for the acquire.py child process.

    - PYTHONIOENCODING: on Windows a piped child gets a locale (cp932)
      stdout, which raises UnicodeEncodeError on acquire.py's non-ASCII
      markers and kills the measurement.
    - IMAGING_SERVER_URL: the port this server actually listens on (the
      child would otherwise assume the default 8799).
    - IMAGING_DATA_DIR: the *resolved* data root. The child runs with
      cwd = the module directory, so a relative IMAGING_DATA_DIR inherited
      as-is would resolve to a different directory there.
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8",
            "IMAGING_SERVER_URL": server_url,
            "IMAGING_DATA_DIR": str(paths.DATA_DIR)}


def _kill_ffmpeg_child() -> None:
    """Make sure frame acquisition is actually stopped.

    An orphaned process still holding the USB device breaks camera
    detection on the next start-up. The macOS-era name is kept even though
    this now simply calls camera.shutdown(). A no-op if the camera was
    never constructed (e.g. the process exits before main() gets to it).
    """
    try:
        if camera is not None:
            camera.shutdown()
    except Exception:
        pass


def _signal_cleanup(signum, frame):
    """On SIGTERM/SIGINT: stop frame acquisition and exit immediately.

    Exits the process directly from the signal handler instead of waiting
    for ThreadingHTTPServer.serve_forever() to return (does not wait for
    other threads to clean up).
    """
    _kill_ffmpeg_child()
    os._exit(0)


atexit.register(_kill_ffmpeg_child)
signal.signal(signal.SIGTERM, _signal_cleanup)
signal.signal(signal.SIGINT, _signal_cleanup)


def _valid_component(s: str) -> bool:
    """Is this safe to use as a single path component (folder/data name)?

    Same rule as acquire.validate_component. Duplicated here to keep this
    server limited to the standard library (no numpy import etc.).
    """
    return bool(s) and s not in (".", "..") and not s.startswith(".") \
        and not any(c in s for c in "/\\\0") and s == Path(s).name


class SpectralJob:
    """Runs one named three-wavelength measurement at a time as a subprocess.

    acquire.py calls back into this server's /api/capture endpoint for each
    shot, so it is launched as a subprocess rather than imported (the same
    execution style as the periodic acquisition loop). Mutual exclusion of
    the measurement sequence itself is guaranteed by acquire.py's own file
    lock.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.result_dir: str | None = None

    def _launch(self, extra: list[str]) -> bool:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False
            self.lines = []
            self.result_dir = None
            # UTF-8 child output, the real server port and the resolved
            # data root (see _child_env); read back as UTF-8 here too.
            env = _child_env()
            self.proc = subprocess.Popen(
                [sys.executable, "-u", str(paths.MODULE_DIR / "acquire.py"), *extra],
                cwd=paths.MODULE_DIR, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", env=env)
            threading.Thread(target=self._pump, args=(self.proc,),
                             daemon=True).start()
            return True

    def start(self, folder: str, name: str, mode: str,
              locked: bool = False, fixed: bool = False,
              white_intensity: int | None = None) -> bool:
        argv = ["--folder", folder, "--name", name, "--mode", mode]
        if locked:
            argv.append("--locked")
        if fixed:
            argv.append("--fixed-intensity")
        if white_intensity is not None:
            argv += ["--white-intensity", str(white_intensity)]
        return self._launch(argv)

    def start_calibrate(self) -> bool:
        return self._launch(["--calibrate"])

    def _pump(self, proc: subprocess.Popen):
        for line in proc.stdout:
            line = line.rstrip("\n")
            with self.lock:
                self.lines.append(line)
                # Pick up the output directory from acquire.py's final
                # "Output: <absolute path>" line (an automatic suffix may
                # have been added if the requested directory already
                # existed).
                if line.startswith("Output: "):
                    try:
                        # relative to the data root, so the value reads
                        # "photos/<folder>/<name>" (what the Analysis tab
                        # expects) regardless of where the data root is
                        rel = Path(line[len("Output: "):]).resolve().relative_to(paths.DATA_DIR)
                        self.result_dir = str(rel)
                    except ValueError:
                        pass
        proc.wait()

    def status(self) -> dict:
        with self.lock:
            if self.proc is None:
                return {"running": False, "exit_code": None,
                        "output_tail": [], "result_dir": None}
            rc = self.proc.poll()
            return {"running": rc is None, "exit_code": rc,
                    "output_tail": self.lines[-12:],
                    "result_dir": self.result_dir if rc == 0 else None}


spectral_job = SpectralJob()

SPECTRAL_LOCK = paths.SPECTRAL_LOCK
RUN_OWNER_PATH = paths.RUN_OWNER_PATH
GEOMETRY_LOCK = paths.OUT_DIR / "locked_geometry.json"
CONDITIONS_DIR = paths.OUT_DIR / "conditions"
_light_mutex = threading.Lock()  # serializes the BLE subprocess


def light_command(args: list[str]) -> dict:
    """Run neewer_light.py as a subprocess (used by the adjustment mode).

    Checks hw.file_lock non-blockingly and leaves the light alone while a
    measurement is running. The lock is held for the whole BLE command, so
    a measurement that starts mid-adjustment is serialized behind the lock
    instead of racing it.
    """
    if not _light_mutex.acquire(blocking=False):
        return {"error": "Another light operation is already running", "status": 409}
    try:
        with hw.file_lock(SPECTRAL_LOCK, timeout=0):
            try:
                r = subprocess.run(
                    [sys.executable, str(paths.MODULE_DIR / "neewer_light.py"), *args],
                    capture_output=True, text=True, timeout=40)
            except subprocess.TimeoutExpired:
                return {"error": "Light control timed out", "status": 500}
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "").strip().splitlines()
                return {"error": msg[-1] if msg else "Light control failed",
                        "status": 500}
            return {"ok": True, "message": r.stdout.strip()}
    except RuntimeError:
        return {"error": "Cannot control the light while a measurement is running", "status": 409}
    finally:
        _light_mutex.release()


def _measurement_idle() -> bool:
    """Check hw.file_lock non-blockingly (the same lock used by acquire.py
    and light_command). True if no measurement sequence is currently
    running. The lock is released immediately after acquiring it, leaving a
    short race window before the caller's own write; acceptable for a
    single-operator local setup.
    """
    try:
        with hw.file_lock(SPECTRAL_LOCK, timeout=0):
            return True
    except RuntimeError:
        return False


def _run_owner_token() -> str | None:
    """The owner token the running acquisition registered (None if absent)."""
    try:
        tok = RUN_OWNER_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tok or None


def control_write_allowed(token: str | None) -> bool:
    """May a /api/control write go ahead?

    Always while no measurement holds the lock. While one does, only a
    write carrying that run's owner token is accepted (the run's own white
    balance fix); anything else -- the Camera tab, a stray script -- is
    refused, because a setting changed mid-run would silently change the
    conditions of the photos still to be taken.
    """
    if _measurement_idle():
        return True
    owner = _run_owner_token()
    return bool(token) and owner is not None and token == owner


def _camera_snapshot() -> dict:
    """Read the current camera settings by logical name (None if unreadable)."""
    return {
        "exposure": camera.get("exposure"),
        "gain": camera.get("gain"),
        "focus": camera.get("focus"),
        "wb": camera.get("wb_temp"),
        "auto_exposure": camera.get("auto_exposure"),
        "auto_focus": camera.get("auto_focus"),
    }


# preset/snapshot key -> hw logical name
_SNAPSHOT_KEYS = {"exposure": "exposure", "gain": "gain", "focus": "focus",
                  "wb": "wb_temp", "auto_exposure": "auto_exposure",
                  "auto_focus": "auto_focus"}


def _apply_camera(saved: dict) -> dict:
    """Apply a saved camera configuration via logical names (skip null
    entries), then read everything back and compare.

    Per the hard rule, white balance stays manual: only the temperature is
    changed, auto white balance is never re-enabled.

    Returns {"ok", "failed_writes", "diff", "read_back"}: failed_writes
    lists the logical names whose set() reported failure; diff lists every
    requested value whose read-back differs ({"key", "requested",
    "read_back"}). Entries the backend cannot read by design (not in
    camera.readable_names, e.g. auto_exposure on DirectShow) are not
    compared. ok is True only when every write succeeded and every
    readable value reads back as requested.
    """
    plan = []   # (key reported in diff, logical name, value)
    for key, conv in (("auto_exposure", bool), ("exposure", int),
                      ("gain", int), ("auto_focus", bool), ("focus", int)):
        if saved.get(key) is not None:
            plan.append((key, _SNAPSHOT_KEYS[key], conv(saved[key])))
    if saved.get("wb") is not None:
        plan.append(("auto_wb", "auto_wb", False))
        plan.append(("wb", "wb_temp", int(saved["wb"])))

    failed = []
    for _key, logical, value in plan:
        if not camera.set(logical, value):
            failed.append(logical)

    read_back = _camera_snapshot()
    readable = set(getattr(camera, "readable_names", hw.CONTROL_NAMES))
    diff = []
    for key, logical, value in plan:
        if logical not in readable:
            continue
        actual = read_back[key] if key in read_back else camera.get(logical)
        if actual != value:
            diff.append({"key": key, "requested": value, "read_back": actual})
    return {"ok": not failed and not diff, "failed_writes": failed,
            "diff": diff, "read_back": read_back}


def _light_args(light: dict) -> list[str]:
    """Convert a condition preset's "light" dict into neewer_light.py argv."""
    action = light.get("action")
    if action == "cct":
        return ["cct", str(int(light["brightness"])), str(int(light["kelvin"]))]
    if action == "hsi":
        return ["hsi", str(int(light["hue"])), str(int(light.get("sat", 100))),
                str(int(light["brightness"]))]
    raise ValueError(f"unknown light action: {action!r}")


# ---- loading the camera reference settings ----

def _load_camera_reference() -> dict | None:
    """Read the reference values from config/camera_reference.json as a
    dict of logical names.

    darwin: converts the existing "reference" key (uvc-util control names;
            the key name is kept as-is for compatibility with the
            calibration tooling - see README.md) into logical names.
    win32 : uses "win32" (logical names; null if not yet calibrated)
            directly.
    Returns None if the file can't be read or converted (matches_reference
    is then None, i.e. "cannot be determined").
    """
    try:
        # encoding is explicit: on Windows the default cp932 locale would
        # raise UnicodeDecodeError on this UTF-8 JSON file even when it is
        # calibrated, making matches_reference silently turn into None.
        doc = json.loads(CAMERA_REFERENCE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if hw.IS_WIN:
        ref = doc.get("win32")
        return ref if isinstance(ref, dict) else None
    uvc_ref = doc.get("reference")
    if not isinstance(uvc_ref, dict):
        return None
    try:
        return {
            "auto_exposure": uvc_ref["auto-exposure-mode"] != 1,
            "exposure": uvc_ref["exposure-time-abs"],
            "gain": uvc_ref["gain"],
            "auto_focus": uvc_ref["auto-focus"] != 0,
            "focus": uvc_ref["focus-abs"],
            "auto_wb": uvc_ref["auto-white-balance-temp"] != 0,
            "wb_temp": uvc_ref["white-balance-temp"],
        }
    except (KeyError, TypeError):
        return None


# ---- /api/health ----

def _health_frame() -> dict:
    with camera.cond:
        ts = camera.frame_ts
        seq = camera.seq
    if ts is None:
        return {"fresh": False, "age_sec": None, "seq": seq}
    age = time.time() - ts
    return {"fresh": age <= 3.0, "age_sec": round(age, 2), "seq": seq}


def _health_ffmpeg() -> dict:
    """Whether the video source is alive (key name kept as "ffmpeg" for
    compatibility; on Windows this reflects the DirectShow capture)."""
    alive = camera.source_alive()
    return {"alive": alive, "pid": camera.source_pid() if alive else None}


def _health_camera() -> dict:
    """Read the 7 logical controls (hw.CONTROL_NAMES) and compare them
    against camera_reference.json.

    "connected" and the reference comparison are judged only on the entries
    in camera.readable_names. On Windows/DirectShow, auto_exposure is
    unreadable by design (always None); requiring every field would make
    "connected" permanently False and cause the preflight check to skip
    every measurement, so unreadable fields are reported as None in
    settings instead.
    """
    settings = {}
    connected = True
    for name in hw.CONTROL_NAMES:
        v = camera.get(name)
        settings[name] = v
        if v is None and name in camera.readable_names:
            connected = False
    reference = _load_camera_reference()
    mismatches = []
    matches_reference = None
    if reference is not None:
        for key, expected in reference.items():
            if key not in camera.readable_names:
                continue
            actual = settings.get(key)
            if actual != expected:
                mismatches.append({"key": key, "expected": expected, "actual": actual})
        matches_reference = len(mismatches) == 0
    # The camera actually held open. Exposed via health so that, on a PC
    # with multiple webcams, "this measurement used the wrong camera" can
    # be detected by health-check tooling (see README.md).
    try:
        device = camera.device_info()
    except Exception:
        device = {"name": None, "index": None, "available": []}
    return {"connected": connected, "settings": settings,
            "matches_reference": matches_reference, "mismatches": mismatches,
            "device": device}


def _health_stream_stale(current_settings: dict) -> bool:
    """Has the live stream drifted from the settings it started with
    (stream_settings)?

    Detects the trap where exposure etc. are changed live but never take
    effect in the stream. Backends where settings apply immediately
    (Windows/DirectShow) have no such concept, so stale_supported is False
    there and this always returns False. Also returns False (undetermined)
    if the start-up snapshot could not be captured.
    """
    if not camera.stale_supported:
        return False
    snap = camera.stream_settings
    if snap is None:
        return False
    return any(current_settings.get(key) != val for key, val in snap.items())


def capture_name(now: datetime | None = None) -> str:
    """A capture file name unique even for several shots per second:
    timestamp to the millisecond plus a random suffix."""
    now = now or datetime.now()
    return (f"sample_{now.strftime('%Y%m%d_%H%M%S')}_"
            f"{now.microsecond // 1000:03d}_{uuid.uuid4().hex[:8]}.jpg")


def save_capture(frame: bytes, directory: Path | None = None) -> Path:
    """Write a frame to a new file (exclusive create; never overwrites)."""
    directory = paths.PHOTOS_DIR if directory is None else directory
    directory.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        path = directory / capture_name()
        try:
            with path.open("xb") as fp:
                fp.write(frame)
            return path
        except FileExistsError:
            continue
    raise OSError("Could not find a free capture file name")


PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".csv": "text/csv; charset=utf-8",
               ".json": "application/json"}
ANALYSIS_TYPES = {".png": "image/png", ".csv": "text/csv; charset=utf-8",
                  ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path == "/stream":
            self._stream()
        elif self.path == "/api/controls":
            self._json(self._read_controls())
        elif self.path == "/api/photos":
            files = sorted(paths.PHOTOS_DIR.glob("*.jpg"), reverse=True)
            self._json({"photos": [f.name for f in files]})
        elif self.path == "/api/spectral/status":
            self._json(spectral_job.status())
        elif self.path == "/api/geometry":
            self._json(self._geometry_status())
        elif self.path == "/api/analysis":
            self._analysis_summary()
        elif self.path == "/api/conditions":
            self._conditions_list()
        elif self.path == "/api/health":
            self._health()
        elif self.path.startswith("/photos/"):
            self._photo_file(unquote(self.path[len("/photos/"):]))
        elif self.path.startswith("/analysis/"):
            self._analysis_file(unquote(self.path[len("/analysis/"):]))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/control":
            self._set_control(payload)
        elif self.path == "/api/capture":
            self._capture()
        elif self.path == "/api/spectral":
            self._start_spectral(payload)
        elif self.path == "/api/geometry/calibrate":
            self._calibrate()
        elif self.path == "/api/light":
            self._light(payload)
        elif self.path == "/api/conditions/save":
            self._conditions_save(payload)
        elif self.path == "/api/conditions/apply":
            self._conditions_apply(payload)
        elif self.path == "/api/conditions/verify":
            self._conditions_verify(payload)
        elif self.path == "/api/conditions/delete":
            self._conditions_delete(payload)
        else:
            self._json({"error": "not found"}, 404)

    # ---- API implementation ----

    def _read_controls(self):
        state = {}
        for pub, (logical, lo, hi) in SLIDERS.items():
            v = camera.get(logical)
            state[pub] = {"value": int(v) if v is not None else None,
                          "min": lo, "max": hi}
        wb = camera.get("wb_temp")
        state["whiteBalance"] = {"value": int(wb) if wb is not None else None,
                                 "min": WB_MIN, "max": WB_MAX}
        for pub, logical in BOOLS.items():
            v = camera.get(logical)
            state[pub] = bool(v) if v is not None else None
        return state

    def _set_control(self, payload):
        name, value = payload.get("name"), payload.get("value")
        if not control_write_allowed(self.headers.get(paths.RUN_TOKEN_HEADER)):
            self._json({"ok": False,
                        "error": "A measurement is running; camera controls "
                                 "are locked until it finishes"}, 409)
            return
        if name in SLIDERS:
            logical, lo, hi = SLIDERS[name]
            v = max(lo, min(hi, int(value)))
            ok = camera.set(logical, v)
            self._json({"ok": ok, "value": v} if ok else {"ok": False}, 200 if ok else 500)
        elif name == "whiteBalance":
            v = max(WB_MIN, min(WB_MAX, int(value)))
            ok = camera.set("wb_temp", v)
            self._json({"ok": ok, "value": v} if ok else {"ok": False}, 200 if ok else 500)
        elif name in BOOLS:
            ok = camera.set(BOOLS[name], bool(value))
            self._json({"ok": ok, "value": bool(value)} if ok else {"ok": False},
                       200 if ok else 500)
        else:
            self._json({"error": "unknown control"}, 400)

    def _photo_file(self, rel: str):
        """Serve a file under photos/ (subfolders allowed, path-traversal safe)."""
        base = paths.PHOTOS_DIR.resolve()
        try:
            p = (base / rel).resolve()
        except (OSError, ValueError):
            self._json({"error": "not found"}, 404)
            return
        ctype = PHOTO_TYPES.get(p.suffix.lower())
        if ctype is None or not p.is_relative_to(base) or p == base:
            self._json({"error": "not found"}, 404)
            return
        self._file(p, ctype)

    def _light(self, payload):
        """Light control for the adjustment mode. action: on/off/cct/hsi."""
        def clamp(key, lo, hi, default=None):
            v = payload.get(key, default)
            if v is None:
                raise ValueError(key)
            return max(lo, min(hi, int(v)))

        action = payload.get("action")
        try:
            if action == "on" or action == "off":
                args = [action]
            elif action == "cct":
                args = ["cct", str(clamp("brightness", 0, 100)),
                        str(clamp("kelvin", 2500, 8500))]
            elif action == "hsi":
                args = ["hsi", str(clamp("hue", 0, 360)),
                        str(clamp("sat", 0, 100, 100)),
                        str(clamp("brightness", 0, 100))]
            else:
                self._json({"error": "unknown action"}, 400)
                return
        except (ValueError, TypeError):
            self._json({"error": "Invalid parameters"}, 400)
            return
        r = light_command(args)
        self._json(r, r.pop("status", 200) if "error" in r else 200)

    def _start_spectral(self, payload):
        folder = str(payload.get("folder") or "").strip()
        name = str(payload.get("name") or "").strip()
        mode = str(payload.get("mode") or "full")
        locked = bool(payload.get("locked"))
        fixed = bool(payload.get("fixed"))
        wi = payload.get("white_intensity")
        if wi in (None, ""):
            white_intensity = None
        else:
            try:
                white_intensity = int(wi)
            except (TypeError, ValueError):
                white_intensity = -1  # rejected by the range check below
            if not (1 <= white_intensity <= 100):
                self._json({"error": "White light intensity must be an integer from 1 to 100"}, 400)
                return
        if not (_valid_component(folder) and _valid_component(name)):
            self._json({"error": "Invalid folder name or data name"}, 400)
            return
        if mode not in ("full", "white"):
            self._json({"error": "Invalid measurement mode"}, 400)
            return
        if mode != "white":
            white_intensity = None  # the UI shouldn't send this, but guard anyway
        if locked and not GEOMETRY_LOCK.is_file():
            self._json({"error": "No locked frame is set (calibrate first)"}, 400)
            return
        if spectral_job.start(folder, name, mode, locked, fixed, white_intensity):
            self._json({"ok": True, "folder": folder, "name": name,
                        "mode": mode, "locked": locked, "fixed": fixed,
                        "white_intensity": white_intensity})
        else:
            self._json({"error": "A measurement is already running"}, 409)

    def _geometry_status(self):
        """Return whether a locked frame exists, and its contents."""
        if not GEOMETRY_LOCK.is_file():
            return {"locked": False}
        try:
            d = json.loads(GEOMETRY_LOCK.read_text(encoding="utf-8"))
            g = d["geometry"]
            return {"locked": True, "calibrated_at": d.get("calibrated_at"),
                    "meniscus_y": g["meniscus"]["y"],
                    "body_x0": g["body"]["x0"], "body_x1": g["body"]["x1"],
                    "liquid_bottom_y": g["liquid"]["bottom_y"],
                    "white_brightness": d.get("white_brightness")}
        except (ValueError, KeyError):
            return {"locked": False, "error": "The locked-frame file is corrupted"}

    def _calibrate(self):
        if spectral_job.start_calibrate():
            self._json({"ok": True})
        else:
            self._json({"error": "A measurement is already running"}, 409)

    # ---- analysis ----

    def _analysis_summary(self):
        analysis_dir = paths.OUT_DIR
        recent_runs = []
        for d in sorted(analysis_dir.glob("spectral_*"), reverse=True):
            summ = d / "summary.json"
            if not summ.is_file():
                continue
            try:
                s = json.loads(summ.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            recent_runs.append({"name": d.name, "captured_at": s.get("captured_at")})
            if len(recent_runs) >= 8:
                break
        self._json({
            "recent_runs": recent_runs,
        })

    def _analysis_file(self, rel: str):
        """Serve only .png/.csv/.json files under analysis/ (path-traversal safe)."""
        base = paths.OUT_DIR.resolve()
        try:
            p = (base / rel).resolve()
        except (OSError, ValueError):
            self._json({"error": "not found"}, 404)
            return
        ctype = ANALYSIS_TYPES.get(p.suffix.lower())
        if ctype is None or not p.is_relative_to(base) or p == base:
            self._json({"error": "not found"}, 404)
            return
        self._file(p, ctype)

    # ---- measurement condition presets ----

    def _conditions_list(self):
        presets = []
        if CONDITIONS_DIR.is_dir():
            for p in sorted(CONDITIONS_DIR.glob("*.json")):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                presets.append({
                    "name": d.get("name", p.stem),
                    "saved_at": d.get("saved_at"),
                    "camera": d.get("camera"),
                    "has_light": d.get("light") is not None,
                    "has_geometry": d.get("geometry") is not None,
                })
        try:
            cam = _camera_snapshot()
        except Exception:
            cam = {"exposure": None, "gain": None, "focus": None, "wb": None,
                   "auto_exposure": None, "auto_focus": None}
        self._json({
            "presets": presets,
            "current": {"camera": cam, "geometry_locked": GEOMETRY_LOCK.is_file()},
        })

    def _conditions_save(self, payload):
        name = str(payload.get("name") or "").strip()
        if not _valid_component(name):
            self._json({"error": "Invalid name"}, 400)
            return
        light = payload.get("light")
        if light is not None and not isinstance(light, dict):
            self._json({"error": "Invalid light"}, 400)
            return
        include_geometry = bool(payload.get("include_geometry"))
        try:
            cam = _camera_snapshot()
        except Exception:
            self._json({"error": "Failed to read the camera settings"}, 500)
            return
        geometry = None
        if include_geometry and GEOMETRY_LOCK.is_file():
            try:
                geometry = json.loads(GEOMETRY_LOCK.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                geometry = None
        try:
            CONDITIONS_DIR.mkdir(parents=True, exist_ok=True)
            doc = {
                "name": name,
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "camera": cam,
                "light": light,
                "geometry": geometry,
            }
            (CONDITIONS_DIR / f"{name}.json").write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            self._json({"error": f"Failed to save: {e}"}, 500)
            return
        self._json({"ok": True, "name": name})

    def _load_condition(self, name: str):
        """Load a preset, validating its name first. Returns None on failure
        (the response has already been sent by this point)."""
        if not _valid_component(name):
            self._json({"error": "Invalid name"}, 400)
            return None
        path = CONDITIONS_DIR / f"{name}.json"
        if not path.is_file():
            self._json({"error": "Preset not found"}, 404)
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            self._json({"error": "Failed to read the preset"}, 500)
            return None

    def _conditions_apply(self, payload):
        name = str(payload.get("name") or "").strip()
        doc = self._load_condition(name)
        if doc is None:
            return
        if not _measurement_idle():
            self._json({"error": "Cannot apply while a measurement is running"}, 409)
            return

        try:
            cam_result = _apply_camera(doc.get("camera") or {})
        except Exception as e:
            self._json({"error": f"Failed to apply the camera settings: {e}"}, 500)
            return
        if not cam_result["ok"]:
            # do not go on to the light/geometry: a half-applied preset
            # must not look like a successful one
            self._json({"error": "The camera did not accept the preset "
                                 "(write failed or read-back differs)",
                        "failed_writes": cam_result["failed_writes"],
                        "diff": cam_result["diff"],
                        "read_back": cam_result["read_back"]}, 500)
            return
        applied_camera = True

        applied_light = False
        if doc.get("light"):
            try:
                args = _light_args(doc["light"])
            except (ValueError, KeyError, TypeError):
                self._json({"error": "The saved light settings are invalid"}, 500)
                return
            r = light_command(args)
            if "error" in r:
                self._json({"error": r["error"]}, r.get("status", 500))
                return
            applied_light = True

        applied_geometry = False
        if doc.get("geometry"):
            try:
                GEOMETRY_LOCK.parent.mkdir(exist_ok=True)
                GEOMETRY_LOCK.write_text(
                    json.dumps(doc["geometry"], ensure_ascii=False, indent=2),
                    encoding="utf-8")
                applied_geometry = True
            except OSError as e:
                self._json({"error": f"Failed to write back the frame: {e}"}, 500)
                return

        try:
            restart_required = _health_stream_stale(
                {name: camera.get(name) for name in hw.CONTROL_NAMES})
        except Exception:
            restart_required = None
        resp = {
            "ok": True,
            "applied": {"camera": applied_camera, "light": applied_light,
                       "geometry": applied_geometry},
            "camera_read_back": cam_result["read_back"],
            "restart_required": restart_required,
        }
        if restart_required:
            # README rule 2: the running stream keeps the settings it was
            # started with, and captured photos come from that stream, so
            # neither the live view nor measurements see the change yet
            # (acquire.py refuses to run until the server is restarted).
            resp["note"] = ("The camera accepted the settings, but the running "
                            "stream (live view AND captured photos) keeps its "
                            "start-up settings. Restart this server before "
                            "measuring.")
        self._json(resp)

    def _conditions_verify(self, payload):
        name = str(payload.get("name") or "").strip()
        doc = self._load_condition(name)
        if doc is None:
            return
        saved = doc.get("camera") or {}
        try:
            current = _camera_snapshot()
        except Exception:
            self._json({"error": "Could not read the current camera settings"}, 500)
            return
        diff = []
        for key in ("exposure", "gain", "focus", "wb", "auto_exposure", "auto_focus"):
            sv = saved.get(key)
            if sv is None:
                continue  # skip fields that could not be read when saving
            cv = current.get(key)
            if sv != cv:
                diff.append({"key": key, "saved": sv, "current": cv})
        self._json({"match": len(diff) == 0, "diff": diff,
                    "note": "Only the camera settings are checked (light/geometry cannot be read back)."})

    def _conditions_delete(self, payload):
        name = str(payload.get("name") or "").strip()
        if not _valid_component(name):
            self._json({"error": "Invalid name"}, 400)
            return
        path = CONDITIONS_DIR / f"{name}.json"
        if path.is_file():
            try:
                path.unlink()
            except OSError as e:
                self._json({"error": f"Failed to delete: {e}"}, 500)
                return
        self._json({"ok": True})

    def _capture(self):
        # Wait for a frame with a seq newer than the one at call time, so we
        # never hand back a stale cached frame (wait_frame(-1) used to
        # return an old frame immediately even when the stream was dead).
        with camera.cond:
            cur = camera.seq
        frame, seq = camera.wait_frame(cur, timeout=5.0)
        if frame is None or seq == cur:
            self._json({"error": "no fresh frame (stream dead?)"}, 503)
            return
        try:
            path = save_capture(frame)
        except OSError as e:
            self._json({"error": f"Failed to save the photo: {e}"}, 500)
            return
        self._json({"ok": True, "file": path.name, "path": str(path)})

    def _health(self):
        """Return the camera server's health status (for health-check
        tooling's pre-flight check; see README.md)."""
        try:
            frame = _health_frame()
        except Exception:
            frame = {"fresh": False, "age_sec": None, "seq": None}
        try:
            ffmpeg_info = _health_ffmpeg()
        except Exception:
            ffmpeg_info = {"alive": False, "pid": None}
        try:
            camera_info = _health_camera()
        except Exception:
            camera_info = {"connected": False,
                           "settings": {k: None for k in hw.CONTROL_NAMES},
                           "matches_reference": None, "mismatches": [],
                           "device": {"name": None, "index": None,
                                      "available": []}}
        try:
            stale = _health_stream_stale(camera_info["settings"])
        except Exception:
            stale = False
        try:
            measurement_running = not _measurement_idle()
        except Exception:
            measurement_running = False
        ok = bool(frame.get("fresh") and ffmpeg_info.get("alive")
                  and camera_info.get("connected")
                  and camera_info.get("matches_reference") is not False
                  and not stale)
        self._json({
            "ok": ok,
            "frame": frame,
            "ffmpeg": ffmpeg_info,
            "camera": camera_info,
            "stream_settings_stale": stale,
            "measurement_running": measurement_running,
        })

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        seq = -1
        try:
            while True:
                frame, seq = camera.wait_frame(seq)
                if frame is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 + f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    global camera
    parser = argparse.ArgumentParser(
        description="Camera control server for spectral-imaging capture rigs.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to bind to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=PORT,
                        help=f"port to listen on (default: {PORT})")
    args = parser.parse_args()

    global server_url
    host = "127.0.0.1" if args.host in ("", "0.0.0.0", "::") else args.host
    if ":" in host:
        host = f"[{host}]"
    server_url = f"http://{host}:{args.port}"
    paths.ensure_dirs()
    camera = hw.CameraSource()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Camera server: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
