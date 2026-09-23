#!/usr/bin/env python3
"""Platform abstraction for the camera and file locking.

Foundation for running the same acquisition code on both macOS and Windows.
Platform-specific details are confined to this module; camera_server.py /
acquire.py only deal with logical control names (exposure, gain, focus,
auto_exposure, auto_focus, auto_wb, wb_temp).

  - macOS: video comes from ffmpeg (AVFoundation), control from uvc-util.
           camera_server.py's ffmpeg and uvc-util can operate on the same camera
           concurrently from separate processes.
  - Windows: both video and control go through OpenCV (DirectShow).
           DirectShow holds the camera exclusively, so only one process
           (= camera_server.py) can open it. Because of this, acquire.py performs
           camera control via camera_server.py over HTTP (capture.get_control /
           capture.set_control).

Logical control names and types:
  auto_exposure : bool  (True = auto)
  exposure      : int   (platform-specific unit)
  gain          : int
  auto_focus    : bool  (True = auto)
  focus         : int
  auto_wb       : bool  (True = auto)
  wb_temp       : int   (Kelvin)
"""
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import paths

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# Logical camera settings. camera_server.py / acquire.py only use these names.
CONTROL_NAMES = ("auto_exposure", "exposure", "gain",
                 "auto_focus", "focus", "auto_wb", "wb_temp")

# Video spec (shared by both platforms)
VIDEO_W, VIDEO_H = 1280, 720
FRAMERATE = 30
DEVICE_NAME = "HD Pro Webcam C920"   # select the target camera by name (default)

# Ways to select a camera on a PC with multiple webcams attached (in priority
# order):
#   1) environment variables SPT_CAMERA_PATH / SPT_CAMERA_NAME ... one-off override
#   2) "win32_device_path" / "win32_device_name" in config/camera_reference.json
#   3) default name DEVICE_NAME
# Name matching is a case-insensitive substring match. **Multiple matches are
# an error** (never run a measurement while it is ambiguous which camera is
# in use).
#
# If two cameras of the same model are attached (this has happened in
# practice), the name cannot distinguish them. In that case, specify part of
# the path (the DirectShow DevicePath, a unique string that includes the
# USB port/instance). Candidates can be listed with list_cameras.py.
CAMERA_NAME_KEY = "win32_device_name"
CAMERA_PATH_KEY = "win32_device_path"


# ==================================================================== locking

@contextmanager
def file_lock(path: Path, timeout: float = 300.0, on_wait=None):
    """Inter-process exclusive lock. Raises RuntimeError if it cannot be acquired.

    Uses fcntl.flock on macOS/Linux and msvcrt.locking on Windows. In both
    cases the OS releases the lock when the process exits, so no stale lock
    is left behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = path.open("w")
    deadline = time.monotonic() + timeout
    announced = False
    try:
        if IS_WIN:
            import msvcrt

            def _try():
                # Non-blocking exclusive lock on the first byte
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)

            def _release():
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            import fcntl

            def _try():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def _release():
                pass  # released automatically on close

        while True:
            try:
                fd.seek(0)
                _try()
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Another measurement is running (could not acquire "
                        f"the lock after waiting {timeout:.0f} seconds)")
                if not announced and on_wait:
                    on_wait()
                    announced = True
                time.sleep(2)
        try:
            yield fd
        finally:
            _release()
    finally:
        fd.close()


# ============================================================ camera backends

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _camera_config() -> dict:
    """Read the camera-selection configuration.

    `config/camera_local.json` (**not tracked in git**) overrides
    `config/camera_reference.json`. Which camera to use depends on how it is
    physically connected to that particular PC, so putting it in a
    repository-shared file would break other PCs.
    """
    doc = _read_json(paths.CAMERA_REFERENCE)
    doc.update(_read_json(paths.CAMERA_LOCAL))
    return doc


def win_camera_name() -> str:
    """Decide which camera name (substring match key) to use.

    Priority: environment variable SPT_CAMERA_NAME > config's
    win32_device_name > default value.
    """
    env = os.environ.get("SPT_CAMERA_NAME")
    if env:
        return env
    name = _camera_config().get(CAMERA_NAME_KEY)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return DEVICE_NAME


def win_camera_path() -> str | None:
    """If the camera is specified by DevicePath, return that substring.

    Priority: environment variable SPT_CAMERA_PATH > config's
    win32_device_path > None.
    Used when several cameras of the same model are attached and cannot be
    distinguished by name.
    """
    env = os.environ.get("SPT_CAMERA_PATH")
    if env:
        return env
    path = _camera_config().get(CAMERA_PATH_KEY)
    if isinstance(path, str) and path.strip():
        return path.strip()
    return None


def list_win_cameras_detailed() -> list[dict]:
    """Enumerate DirectShow video input devices in enumeration order
    (= OpenCV index).

    Each element is {"index": int, "name": str, "path": str|None}.
    path is the DevicePath (a unique string including the USB VID/PID and
    port instance). Used to disambiguate identically-named cameras.
    """
    from comtypes import GUID
    from comtypes.persist import IPropertyBag
    from pygrabber.dshow_graph import SystemDeviceEnum
    from pygrabber.dshow_ids import DeviceCategories

    sde = SystemDeviceEnum()
    enumerator = sde.system_device_enum.CreateClassEnumerator(
        GUID(DeviceCategories.VideoInputDevice), dwFlags=0)
    devices: list[dict] = []
    if enumerator is None:
        return devices
    try:
        moniker, count = enumerator.Next(1)
    except ValueError:
        return devices
    while count > 0:
        bag = moniker.BindToStorage(0, 0, IPropertyBag._iid_).QueryInterface(IPropertyBag)
        try:
            name = bag.Read("FriendlyName", pErrorLog=None)
        except Exception:
            name = "(unknown)"
        try:
            path = bag.Read("DevicePath", pErrorLog=None)
        except Exception:
            path = None      # virtual cameras etc. may not have a DevicePath
        devices.append({"index": len(devices), "name": name, "path": path})
        try:
            moniker, count = enumerator.Next(1)
        except ValueError:
            break
    return devices


def list_win_cameras() -> list[str]:
    """Enumerate DirectShow video input device names in enumeration order
    (= OpenCV index)."""
    return [d["name"] for d in list_win_cameras_detailed()]


def resolve_win_camera() -> dict:
    """Decide on a single camera to use. Returns
    {"index","name","path","devices"}.

    pygrabber's enumeration order matches the OpenCV (CAP_DSHOW) index.

    To reliably open the intended camera on a PC with multiple webcams
    attached, both of the following are always treated as errors (never
    silently open a different camera):

    - **Not found.** Must not fall back to index 0.
      An actual incident occurred on 2026-07-27: with the C920 unplugged,
      camera_server.py grabbed a different webcam (ELECOM) at index 0 and streamed
      from it without anyone noticing.
    - **Multiple matches.** E.g. two cameras of the same model (this has
      happened), or a selector that is too loose. It is better to stop than
      to run a measurement while it is ambiguous which camera is in use.
      -> Narrow it down to one camera with a path selector
        (win32_device_path / SPT_CAMERA_PATH). Candidates can be listed
        with `python list_cameras.py`.
    """
    devices = list_win_cameras_detailed()
    want_path = win_camera_path()
    want_name = win_camera_name()

    if want_path:
        key, kind, matches = want_path, "path", [
            d for d in devices
            if d["path"] and want_path.lower() in d["path"].lower()]
    else:
        key, kind, matches = want_name, "name", [
            d for d in devices if want_name.lower() in d["name"].lower()]

    listing = [f"index {d['index']}: {d['name']}" for d in devices]
    if not matches:
        raise RuntimeError(
            f"Camera ({kind} selector '{key}') not found among DirectShow "
            f"devices (detected: {listing}). Unplug and re-plug the USB "
            f"cable directly into the PC, then see README.md for the "
            f"camera-recovery steps.")
    if len(matches) > 1:
        found = [f"index {d['index']}: {d['name']} path={d['path']}" for d in matches]
        raise RuntimeError(
            f"Camera ({kind} selector '{key}') matched multiple devices "
            f"({found}). Set '{CAMERA_PATH_KEY}' in "
            f"config/camera_reference.json to part of a device path that "
            f"matches only one device (candidates: "
            f"`python list_cameras.py`; use --snapshot to tell "
            f"which one is the measurement camera).")
    d = matches[0]
    return {"index": d["index"], "name": d["name"], "path": d["path"],
            "devices": devices}


class _WinCamera:
    """Holds the C920 exclusively via OpenCV (DirectShow) and keeps the
    latest JPEG frame."""

    # DirectShow applies set() to an open cap immediately, so there is no
    # "not reflected in the stream" (stale) concept like on macOS with its
    # persistent ffmpeg process.
    stale_supported = False

    # Items DirectShow cannot get by design (auto_exposure always reads
    # back -1). /api/health's connected check and reference comparison only
    # look at the items in this set. If other items also turn out to be
    # unreadable on real Windows hardware, remove them from here too (see
    # the calibration notes in README.md).
    readable_names = tuple(n for n in CONTROL_NAMES if n != "auto_exposure")

    # logical name -> cv2 property, with encode/decode (bool: auto=True)
    def __init__(self):
        import cv2
        self._cv2 = cv2
        # Decoding is based on measurements with the C920 + DirectShow:
        #   AUTO_EXPOSURE: get always returns -1 (unsupported). set uses 0.25/0.75.
        #   AUTOFOCUS:     get returns CameraControl_Flags (1=auto, 2=manual).
        #   AUTO_WB:       plain 0/1.
        # When get returns a negative value (unsupported), return None.
        self._props = {
            "auto_exposure": (cv2.CAP_PROP_AUTO_EXPOSURE,
                              lambda b: 0.75 if b else 0.25,
                              lambda v: None if v < 0 else v >= 0.5),
            "exposure":      (cv2.CAP_PROP_EXPOSURE, int, int),
            "gain":          (cv2.CAP_PROP_GAIN, int, int),
            "auto_focus":    (cv2.CAP_PROP_AUTOFOCUS,
                              lambda b: 1 if b else 0,
                              lambda v: None if v < 0 else int(v) == 1),
            "focus":         (cv2.CAP_PROP_FOCUS, int, int),
            "auto_wb":       (cv2.CAP_PROP_AUTO_WB,
                              lambda b: 1 if b else 0,
                              lambda v: None if v < 0 else v >= 0.5),
            # On DirectShow, the WB color temperature maps to
            # WHITE_BALANCE_BLUE_U in K (measured on the C920;
            # CAP_PROP_WB_TEMPERATURE is unsupported and reads -1)
            "wb_temp":       (cv2.CAP_PROP_WHITE_BALANCE_BLUE_U, int, int),
        }
        chosen = resolve_win_camera()
        idx = chosen["index"]
        self.device_name = chosen["name"]
        self.device_index = idx
        self.device_path = chosen["path"]
        self.available_devices = [d["name"] for d in chosen["devices"]]
        # Always log which camera was grabbed (so that on a PC with multiple
        # webcams, "measured with the wrong camera" can be verified after
        # the fact).
        print(f"Camera selected: index {idx} '{self.device_name}' "
              f"path={self.device_path} / detected: {self.available_devices}",
              file=sys.stderr)
        self._cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not self._cap.isOpened():
            raise RuntimeError(
                f"Cannot open camera (index {idx} '{self.device_name}'). "
                f"Check whether another app (Teams, Camera app, etc.) has "
                f"it open.")
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_W)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_H)
        self._cap.set(cv2.CAP_PROP_FPS, FRAMERATE)
        self._lock = threading.Lock()          # serialize reads/writes to cap
        self.frame: bytes | None = None
        self.frame_ts: float | None = None
        self.seq = 0
        self.cond = threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        cv2 = self._cv2
        while True:
            with self._lock:
                ok, frame = self._cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            ok, jpg = cv2.imencode(".jpg", frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                continue
            with self.cond:
                self.frame = jpg.tobytes()
                self.frame_ts = time.time()
                self.seq += 1
                self.cond.notify_all()

    def wait_frame(self, last_seq: int, timeout: float = 5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq, timeout)
            return self.frame, self.seq

    def get(self, name: str):
        prop, _enc, dec = self._props[name]
        with self._lock:
            raw = self._cap.get(prop)
        return dec(raw)

    def set(self, name: str, value) -> bool:
        prop, enc, _dec = self._props[name]
        with self._lock:
            return bool(self._cap.set(prop, enc(value)))

    def source_alive(self) -> bool:
        """Whether the video source (cap) is open."""
        return bool(self._cap.isOpened())

    def source_pid(self) -> int | None:
        """OpenCV has no separate process, so this is always None."""
        return None

    def device_info(self) -> dict:
        """The camera actually in use (exposed via /api/health for
        diagnostics to cross-check)."""
        return {"name": self.device_name, "index": self.device_index,
                "path": self.device_path,
                "available": list(self.available_devices)}

    def shutdown(self) -> None:
        """Release cap (used for cleanup on process exit)."""
        with self._lock:
            self._cap.release()


# One device row of `uvc-util -d`, e.g.
#            0 0x046d:0x082d   0x14200000       1.00    HD Pro Webcam C920
_UVC_ROW = re.compile(
    r"^\s*(\d+)\s+0x[0-9a-fA-F]+:0x[0-9a-fA-F]+\s+\S+\s+\S+\s+(.+?)\s*$")


def parse_uvc_device_list(text: str) -> list[dict]:
    """Parse `uvc-util -d` output into [{"index": int, "name": str}]."""
    out = []
    for line in text.splitlines():
        m = _UVC_ROW.match(line)
        if m:
            out.append({"index": int(m.group(1)), "name": m.group(2)})
    return out


def resolve_uvc_index(devices: list[dict], name: str) -> int:
    """Pick the uvc-util index of the camera ffmpeg opens by `name`.

    ffmpeg (AVFoundation) opens the camera by name, so uvc-util must
    control that same camera; its index is not necessarily 0. Case-
    insensitive substring match, like the Windows selector. Zero or
    multiple matches are errors (never control a different camera than the
    one being streamed).
    """
    matches = [d for d in devices if name.lower() in d["name"].lower()]
    listing = [f"index {d['index']}: {d['name']}" for d in devices]
    if not matches:
        raise RuntimeError(
            f"Camera '{name}' not found by uvc-util (detected: {listing}). "
            f"Re-plug the camera and see README.md.")
    if len(matches) > 1:
        raise RuntimeError(
            f"Camera name '{name}' matches several uvc-util devices "
            f"({listing}); camera control would be ambiguous. Leave only "
            f"one such camera connected.")
    return matches[0]["index"]


class _MacCamera:
    """Persistent ffmpeg (AVFoundation) + uvc-util control (existing macOS
    implementation)."""

    # While ffmpeg is running, live uvc-util changes are not reflected in
    # the stream (a known pitfall), so the settings snapshot taken right
    # after startup must be compared against later reads (stale detection).
    stale_supported = True

    # uvc-util can read all 7 items
    readable_names = CONTROL_NAMES

    UVC = paths.uvc_util_path()
    # logical name -> (uvc-util name, set encoder, get decoder)
    _MAP = {
        "auto_exposure": ("auto-exposure-mode",
                          lambda b: "8" if b else "1", lambda s: s != "1"),
        "exposure":      ("exposure-time-abs", str, int),
        "gain":          ("gain", str, int),
        "auto_focus":    ("auto-focus",
                          lambda b: "true" if b else "false",
                          lambda s: s not in ("0", "false")),
        "focus":         ("focus-abs", str, int),
        "auto_wb":       ("auto-white-balance-temp",
                          lambda b: "true" if b else "false",
                          lambda s: s not in ("0", "false")),
        "wb_temp":       ("white-balance-temp", str, int),
    }

    def __init__(self):
        self.frame: bytes | None = None
        self.frame_ts: float | None = None
        self.seq = 0
        self.cond = threading.Condition()
        self.proc: "subprocess.Popen | None" = None
        self.stream_settings: dict | None = None
        # resolve which uvc-util index is the camera ffmpeg streams (by
        # name) before anything reads or writes a control
        self.uvc_index = self._resolve_uvc_index()
        print(f"Camera selected: '{DEVICE_NAME}' (uvc-util index "
              f"{self.uvc_index})", file=sys.stderr)
        threading.Thread(target=self._pump, daemon=True).start()

    def _resolve_uvc_index(self) -> int:
        import subprocess
        try:
            r = subprocess.run([self.UVC, "-d"], capture_output=True,
                               text=True, timeout=10)
        except FileNotFoundError:
            raise RuntimeError(
                f"uvc-util not found at {self.UVC}. Install it "
                f"(https://github.com/jtfrey/uvc-util) and put it on PATH "
                f"or set UVC_UTIL — see README.md")
        if r.returncode != 0:
            raise RuntimeError(f"uvc-util -d failed: {r.stderr.strip()}")
        return resolve_uvc_index(parse_uvc_device_list(r.stdout), DEVICE_NAME)

    def _snapshot_settings(self) -> dict | None:
        """Measure and snapshot all 7 logical items (used for stale detection).

        If even one item cannot be read, return None (to avoid a partial
        comparison).
        """
        out = {}
        try:
            for name in CONTROL_NAMES:
                v = self.get(name)
                if v is None:
                    return None
                out[name] = v
        except RuntimeError as e:
            print(f"Cannot snapshot camera settings: {e}", file=sys.stderr)
            return None
        return out

    def _pump(self):
        import subprocess
        while True:
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "avfoundation", "-framerate", str(FRAMERATE),
                 "-video_size", f"{VIDEO_W}x{VIDEO_H}", "-pixel_format", "nv12",
                 "-i", DEVICE_NAME,
                 "-c:v", "mjpeg", "-q:v", "4", "-f", "mjpeg", "-"],
                stdout=subprocess.PIPE)
            self.proc = proc
            # Record the settings right after ffmpeg starts, so that
            # /api/health can later check whether the measured values have
            # drifted from them (used to detect the "live exposure change
            # not reflected" pitfall).
            self.stream_settings = self._snapshot_settings()
            buf = b""
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi < 0:
                        buf = b""
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi < 0:
                        buf = buf[soi:]
                        break
                    jpeg, buf = buf[soi:eoi + 2], buf[eoi + 2:]
                    with self.cond:
                        self.frame = jpeg
                        self.frame_ts = time.time()
                        self.seq += 1
                        self.cond.notify_all()
            proc.wait()
            time.sleep(1)  # restart ffmpeg if the camera was disconnected etc.

    def wait_frame(self, last_seq: int, timeout: float = 5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq, timeout)
            return self.frame, self.seq

    def _uvc(self, args):
        import subprocess
        try:
            return subprocess.run([self.UVC, "-I", str(self.uvc_index), *args],
                                  capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            raise RuntimeError(
                f"uvc-util not found at {self.UVC}. Install it "
                f"(https://github.com/jtfrey/uvc-util) and put it on PATH "
                f"or set UVC_UTIL — see README.md")

    def get(self, name: str):
        uvc_name, _enc, dec = self._MAP[name]
        r = self._uvc(["-o", uvc_name])
        if r.returncode != 0:
            return None
        return dec(r.stdout.strip())

    def set(self, name: str, value) -> bool:
        uvc_name, enc, _dec = self._MAP[name]
        r = self._uvc(["-s", f"{uvc_name}={enc(value)}"])
        return r.returncode == 0

    def source_alive(self) -> bool:
        """Whether the ffmpeg child process is alive."""
        return self.proc is not None and self.proc.poll() is None

    def source_pid(self) -> int | None:
        return self.proc.pid if self.source_alive() else None

    def device_info(self) -> dict:
        """ffmpeg opens the camera by name; index is uvc-util's index of
        that same camera (resolved by name at start-up)."""
        return {"name": DEVICE_NAME, "index": self.uvc_index, "available": []}

    def shutdown(self) -> None:
        """Make sure the ffmpeg child process terminates (an orphaned
        process still holding the USB device would break camera detection
        on the next startup)."""
        import subprocess
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass


def CameraSource():
    """Return the camera implementation for this platform."""
    return _WinCamera() if IS_WIN else _MacCamera()
