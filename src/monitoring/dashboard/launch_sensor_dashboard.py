import json
import socket
import sys
import threading
import time
import os
import csv
import ipaddress
import secrets
import logging
import webbrowser
import asyncio
from urllib.parse import urlsplit
import cv2
import numpy as np
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form, Depends, Header, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel
from zeroconf import Zeroconf, ServiceBrowser

logger = logging.getLogger(__name__)

# --- AprilTag detector (shared, default: tag36h11) ---
APRILTAG_DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
APRILTAG_PARAMS = cv2.aruco.DetectorParameters()
APRILTAG_PARAMS.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
APRILTAG_DETECTOR = cv2.aruco.ArucoDetector(APRILTAG_DICT, APRILTAG_PARAMS)

# --- Pose smoothing state (per-marker) ---
_pose_history = {}
_pose_lock = threading.Lock()
SMOOTH_ALPHA = 0.45             # 0=no update, 1=no smoothing
POS_JUMP_THRESH_M = 0.20        # reject if translation jumps > 20cm in one tick
ROT_JUMP_THRESH_DEG = 50.0      # reject if rotation flips > 50° in one tick
POSE_TIMEOUT_S = 1.0            # forget marker if not seen for > 1s

# --- Zero (reference) poses per marker ---
_zero_refs = {}
_zero_lock = threading.Lock()


def _R_to_quat(m):
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        S = np.sqrt(tr + 1.0) * 2
        qw = 0.25 * S
        qx = (m[2, 1] - m[1, 2]) / S
        qy = (m[0, 2] - m[2, 0]) / S
        qz = (m[1, 0] - m[0, 1]) / S
    elif (m[0, 0] > m[1, 1]) and (m[0, 0] > m[2, 2]):
        S = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        qw = (m[2, 1] - m[1, 2]) / S
        qx = 0.25 * S
        qy = (m[0, 1] + m[1, 0]) / S
        qz = (m[0, 2] + m[2, 0]) / S
    elif m[1, 1] > m[2, 2]:
        S = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        qw = (m[0, 2] - m[2, 0]) / S
        qx = (m[0, 1] + m[1, 0]) / S
        qy = 0.25 * S
        qz = (m[1, 2] + m[2, 1]) / S
    else:
        S = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        qw = (m[1, 0] - m[0, 1]) / S
        qx = (m[0, 2] + m[2, 0]) / S
        qy = (m[1, 2] + m[2, 1]) / S
        qz = 0.25 * S
    return np.array([qw, qx, qy, qz])


def _quat_to_R(q):
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ])


def _slerp_quat(q1, q2, t):
    q1 = q1 / (np.linalg.norm(q1) + 1e-12)
    q2 = q2 / (np.linalg.norm(q2) + 1e-12)
    dot = float(np.dot(q1, q2))
    if dot < 0:
        q2 = -q2
        dot = -dot
    if dot > 0.9995:
        q = q1 + t * (q2 - q1)
        return q / (np.linalg.norm(q) + 1e-12)
    dot = max(-1.0, min(1.0, dot))
    theta_0 = np.arccos(dot)
    sin_theta_0 = np.sin(theta_0)
    theta = theta_0 * t
    sin_theta = np.sin(theta)
    s1 = np.cos(theta) - dot * sin_theta / sin_theta_0
    s2 = sin_theta / sin_theta_0
    return s1 * q1 + s2 * q2


def _angle_between_R_deg(R1, R2):
    R_rel = R1 @ R2.T
    cos = (np.trace(R_rel) - 1.0) / 2.0
    cos = float(max(-1.0, min(1.0, cos)))
    return float(np.degrees(np.arccos(cos)))


def _smooth_pose(tag_id, tvec, R, alpha=SMOOTH_ALPHA):
    """EMA smoothing + outlier rejection. Returns (tvec_out, R_out, was_outlier)."""
    now = time.time()
    with _pose_lock:
        last = _pose_history.get(tag_id)
        if last is None or (now - last["t"]) > POSE_TIMEOUT_S:
            _pose_history[tag_id] = {"tvec": tvec.copy(), "R": R.copy(), "t": now}
            return tvec, R, False

        last_t = last["tvec"]
        last_R = last["R"]
        pos_jump = float(np.linalg.norm(tvec - last_t))
        rot_jump = _angle_between_R_deg(R, last_R)
        if pos_jump > POS_JUMP_THRESH_M or rot_jump > ROT_JUMP_THRESH_DEG:
            # reject this tick, hold previous pose, refresh timestamp so we don't time out
            _pose_history[tag_id]["t"] = now
            return last_t, last_R, True

        smoothed_t = alpha * tvec + (1.0 - alpha) * last_t
        q_last = _R_to_quat(last_R)
        q_new = _R_to_quat(R)
        q_smooth = _slerp_quat(q_last, q_new, alpha)
        q_smooth = q_smooth / (np.linalg.norm(q_smooth) + 1e-12)
        smoothed_R = _quat_to_R(q_smooth)
        _pose_history[tag_id] = {"tvec": smoothed_t.copy(), "R": smoothed_R.copy(), "t": now}
        return smoothed_t, smoothed_R, False


def _rotation_to_euler_deg(R):
    sy = float(np.sqrt(R[0, 0] ** 2 + R[1, 0] ** 2))
    if sy > 1e-6:
        roll = float(np.degrees(np.arctan2(R[2, 1], R[2, 2])))
        pitch = float(np.degrees(np.arctan2(-R[2, 0], sy)))
        yaw = float(np.degrees(np.arctan2(R[1, 0], R[0, 0])))
    else:
        roll = float(np.degrees(np.arctan2(-R[1, 2], R[1, 1])))
        pitch = float(np.degrees(np.arctan2(-R[2, 0], sy)))
        yaw = 0.0
    return roll, pitch, yaw


def _detect_apriltags_sync(jpeg_bytes, tag_size_m, frame_w, frame_h, smooth=True):
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return []

    # Approximate intrinsics: fx ≈ 0.85 * width for ~60° HFOV webcam.
    # Without true calibration distance has bias, but relative motion / angles still useful.
    fx = fy = 0.85 * frame_w
    cam_matrix = np.array([
        [fx, 0.0, frame_w / 2.0],
        [0.0, fy, frame_h / 2.0],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)
    dist = np.zeros((5, 1), dtype=np.float64)

    corners, ids, _ = APRILTAG_DETECTOR.detectMarkers(gray)
    out = []
    if ids is None:
        return out

    s = tag_size_m / 2.0
    obj_pts = np.array([
        [-s,  s, 0.0],
        [ s,  s, 0.0],
        [ s, -s, 0.0],
        [-s, -s, 0.0],
    ], dtype=np.float32)

    for i, marker_id in enumerate(ids):
        img_pts = corners[i].reshape(4, 2).astype(np.float32)
        tag_id = int(marker_id.item() if hasattr(marker_id, "item") else marker_id[0])

        # Try iterative refinement seeded by the previous pose to suppress IPPE_SQUARE flip-flop.
        with _pose_lock:
            last = _pose_history.get(tag_id)
        seeded = False
        if smooth and last is not None and (time.time() - last["t"]) <= POSE_TIMEOUT_S:
            try:
                rvec_init, _ = cv2.Rodrigues(last["R"])
                tvec_init = last["tvec"].reshape(3, 1)
                ok, rvec, tvec = cv2.solvePnP(
                    obj_pts, img_pts, cam_matrix, dist,
                    rvec=rvec_init, tvec=tvec_init,
                    useExtrinsicGuess=True,
                    flags=cv2.SOLVEPNP_ITERATIVE,
                )
                seeded = ok
            except cv2.error:
                seeded = False
        if not seeded:
            try:
                ok, rvec, tvec = cv2.solvePnP(
                    obj_pts, img_pts, cam_matrix, dist,
                    flags=cv2.SOLVEPNP_IPPE_SQUARE,
                )
            except cv2.error:
                ok = False
            if not ok:
                continue

        R, _ = cv2.Rodrigues(rvec)
        tvec_flat = tvec.reshape(3)
        was_outlier = False
        if smooth:
            tvec_flat, R, was_outlier = _smooth_pose(tag_id, tvec_flat, R)

        roll, pitch, yaw = _rotation_to_euler_deg(R)

        # Delta vs zero reference (in marker's zero-frame)
        delta = None
        with _zero_lock:
            ref = _zero_refs.get(tag_id)
        if ref is not None:
            R_zero = ref["R"]
            t_zero = ref["tvec"]
            dt_world = tvec_flat - t_zero
            dt_local = R_zero.T @ dt_world
            R_delta = R_zero.T @ R
            dr, dp, dy = _rotation_to_euler_deg(R_delta)
            delta = {
                "dx_mm": float(dt_local[0] * 1000.0),
                "dy_mm": float(dt_local[1] * 1000.0),
                "dz_mm": float(dt_local[2] * 1000.0),
                "droll": dr,
                "dpitch": dp,
                "dyaw": dy,
            }

        out.append({
            "id": tag_id,
            "corners": img_pts.tolist(),
            "tx_mm": float(tvec_flat[0] * 1000.0),
            "ty_mm": float(tvec_flat[1] * 1000.0),
            "tz_mm": float(tvec_flat[2] * 1000.0),
            "roll": roll,
            "pitch": pitch,
            "yaw": yaw,
            "outlier": bool(was_outlier),
            "delta": delta,
        })
    return out

# --- Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # src/monitoring/dashboard
MONITORING_DIR = os.path.dirname(BASE_DIR)                     # src/monitoring
REPO_ROOT = os.path.dirname(os.path.dirname(MONITORING_DIR))   # repository root
STATIC_DIR = os.path.join(BASE_DIR, "static")


class DashboardConfigError(RuntimeError):
    """An existing monitoring config file cannot be used.

    Local equivalent of ``src.config.ConfigError`` (not imported, so the
    dashboard keeps running as a standalone script without the repository's
    package on sys.path). Raised instead of falling back to defaults, so a
    typo in config.yaml cannot silently move the dashboard to other ports than
    the Pis are configured for.
    """


def _load_dashboard_ports(candidates=None):
    """Load the sensor_dashboard port settings.

    Single source of truth is src/monitoring/config.yaml. If that file does not
    exist (not yet created by the user), fall back to config.example.yaml, and
    finally to the built-in defaults below. The first file that *exists* is
    used; if it cannot be read or parsed, or its values are not port numbers,
    DashboardConfigError is raised — there is no silent fallback. This loader
    is PC-side only — the Pi agent (pi_sensor_agent.py) intentionally does not
    depend on PyYAML and reads its own settings from environment variables.
    """
    defaults = {"web_port": 8000, "tcp_port": 50001}
    if candidates is None:
        candidates = [
            os.path.join(MONITORING_DIR, "config.yaml"),
            os.path.join(MONITORING_DIR, "config.example.yaml"),
        ]

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            import yaml
        except ImportError as e:
            raise DashboardConfigError(
                f"PyYAML is not installed, so {path} cannot be read "
                f"(run: pip install -r src/monitoring/requirements.txt). Refusing to fall back to "
                f"built-in defaults."
            ) from e
        try:
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            raise DashboardConfigError(f"Could not read {path}: {e}") from e
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise DashboardConfigError(
                f"{path}: the top level must be a mapping, got {type(loaded).__name__}"
            )
        section = loaded.get("sensor_dashboard")
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise DashboardConfigError(
                f"{path}: 'sensor_dashboard' must be a mapping, got {type(section).__name__}"
            )
        merged = dict(defaults)
        for key in defaults:
            if key not in section:
                continue
            value = section[key]
            if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 65535):
                raise DashboardConfigError(
                    f"{path}: sensor_dashboard.{key} must be a port number (1-65535), got {value!r}"
                )
            merged[key] = value
        return merged
    logger.warning(
        "Neither config.yaml nor config.example.yaml found under %s; using built-in defaults %s",
        MONITORING_DIR, defaults,
    )
    return defaults


_ports = _load_dashboard_ports()
TCP_PORT = _ports["tcp_port"]
WEB_PORT = _ports["web_port"]
# Web server bind address. Default is localhost only (keeps dangerous API/WS
# operations unreachable from the network). Only set
# SENSOR_DASHBOARD_HOST=0.0.0.0 (or a specific LAN IP) explicitly if a browser
# on another machine needs to open the dashboard.
WEB_HOST = os.environ.get("SENSOR_DASHBOARD_HOST", "127.0.0.1")
# TCP server that Pi sensor agents connect to. The Pi is a separate host, so
# the default is to listen on all interfaces.
TCP_HOST = os.environ.get("SENSOR_DASHBOARD_TCP_HOST", "0.0.0.0")
# Shared secret. Only when set does /api/start and /api/end require a matching
# token (the flow runner / GUI sends it from the same environment variable).
AUTH_TOKEN = os.environ.get("SENSOR_DASHBOARD_TOKEN") or None
# Host header allow-list (DNS-rebinding protection). localhost variants plus
# (when publishing on the LAN) the real bind IP are allowed.
_allowed = {"localhost", "127.0.0.1", "::1"}
if WEB_HOST not in ("0.0.0.0", "::", ""):
    _allowed.add(WEB_HOST)
for h in os.environ.get("SENSOR_DASHBOARD_ALLOWED_HOSTS", "").split(","):
    if h.strip():
        _allowed.add(h.strip())
ALLOWED_HOSTS = sorted(_allowed)


def _parse_ip_allowlist(raw: str):
    """Parse a comma-separated list of IPs / CIDR networks; raise on a bad entry."""
    nets = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as e:
            raise DashboardConfigError(
                f"SENSOR_DASHBOARD_PI_ALLOWED_IPS: {entry!r} is not an IP address or network: {e}"
            ) from e
    return nets


# Sensor-ingestion protection (TCP :50001). Both are optional and independent:
# - SENSOR_DASHBOARD_PI_ALLOWED_IPS: comma-separated IPs/CIDRs (e.g.
#   "192.0.2.21,192.0.2.0/28"); connections from any other address are closed
#   before a byte is read. Empty = accept any address (the historical default).
# - SENSOR_DASHBOARD_PI_TOKEN: shared secret; when set, the first line of every
#   connection must be {"type": "auth", "token": "<secret>"} (the Pi agent
#   sends it when SENSOR_AGENT_TOKEN is set), otherwise the connection is closed.
PI_ALLOWED_NETS = _parse_ip_allowlist(os.environ.get("SENSOR_DASHBOARD_PI_ALLOWED_IPS", ""))
PI_TOKEN = os.environ.get("SENSOR_DASHBOARD_PI_TOKEN") or None
# How long a connection may take to send its auth line.
PI_AUTH_TIMEOUT_S = 5.0
# TCP-ingestion DoS protection (prevents an unbounded buffer if no newline ever arrives).
TCP_MAX_LINE = 64 * 1024        # limit for a single line (one JSON message)
TCP_MAX_BUFFER = 256 * 1024     # limit for the whole unprocessed buffer
# Upper bound for a single POST /api/upload_video body (a browser-recorded clip).
# Prevents an unbounded write to the save directory; override if longer sessions
# are recorded in-page.
UPLOAD_MAX_BYTES = int(os.environ.get("SENSOR_DASHBOARD_MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))


def _is_loopback(host: str) -> bool:
    """Whether a WebSocket/HTTP client connected from the local machine. Used to authorize privileged operations."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _sanitize_name(s, maxlen: int = 128) -> str:
    """Restrict a string used in a filename to alphanumerics, '_' and '-' (defends against path traversal / formula injection)."""
    return "".join(c for c in (s or "") if c.isalnum() or c in "_-")[:maxlen]


def _token_ok(candidate) -> bool:
    """Constant-time comparison of a client-supplied token against AUTH_TOKEN."""
    if not AUTH_TOKEN:
        return True
    return isinstance(candidate, str) and secrets.compare_digest(candidate, AUTH_TOKEN)


def _origin_allowed(origin: str | None, client_host: str) -> bool:
    """Whether a WebSocket handshake's Origin header may open /ws.

    Browsers always send Origin on a WebSocket handshake, so checking it is what
    stops cross-site WebSocket hijacking: a page on evil.example could otherwise
    open ws://localhost:8000/ws from the operator's browser (the same-origin
    policy does not apply to WebSockets) and drive update_config +
    start_recording to write CSV files to an arbitrary directory. The allow-list
    is the same host set used by TrustedHostMiddleware
    (`SENSOR_DASHBOARD_ALLOWED_HOSTS`), so no new configuration is needed.

    A missing Origin means a non-browser client (curl, python-websockets, the
    flow runner); that is accepted only from the local machine.
    """
    if not origin:
        return _is_loopback(client_host)
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    return host in set(ALLOWED_HOSTS)


async def require_token(x_auth_token: str | None = Header(default=None)):
    """Require a matching token only when AUTH_TOKEN is set (pass through otherwise)."""
    if not _token_ok(x_auth_token):
        raise HTTPException(status_code=401, detail="invalid or missing auth token")


def _same_origin(source: str | None, host_header: str | None) -> bool:
    """Whether an Origin/Referer URL points at this very dashboard.

    Same-origin means the URL's ``host[:port]`` equals the request's Host
    header (which TrustedHostMiddleware has already checked against
    ALLOWED_HOSTS) and the scheme is http(s). ``Origin: null`` (sandboxed
    frames, file://) is never same-origin.
    """
    if not source or source == "null" or not host_header:
        return False
    try:
        parsed = urlsplit(source)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return parsed.netloc.lower() == host_header.lower()


async def require_same_origin(request: Request):
    """Reject cross-site browser requests to state-changing endpoints.

    A browser attaches ``Origin`` to every cross-site POST (and ``Referer``
    in most other cases), so a page on another site cannot make the operator's
    browser start/stop a recording, upload files or reset tag zeros: an HTML
    form or ``fetch(..., {mode: "no-cors"})`` needs no token when
    SENSOR_DASHBOARD_TOKEN is unset and no request body for /api/end. The
    check uses Origin if present, else Referer. A request with neither is a
    non-browser client (the flow runner, the GUI, curl) and is let through;
    it is still subject to the token when one is configured.
    """
    host = request.headers.get("host")
    origin = request.headers.get("origin")
    if origin is not None:
        ok = _same_origin(origin, host)
        source = origin
    else:
        referer = request.headers.get("referer")
        if referer is None:
            return
        ok = _same_origin(referer, host)
        source = referer
    if not ok:
        logger.warning("Rejected cross-origin %s %s from %r", request.method, request.url.path, source)
        raise HTTPException(status_code=403, detail="cross-origin request refused")


# Dependencies for every endpoint that changes dashboard state or writes files.
STATE_CHANGING = [Depends(require_same_origin), Depends(require_token)]

def _find_agent_path():
    candidates = [
        os.path.join(BASE_DIR, "pi_sensor_agent.py"),
        os.path.join(os.path.dirname(BASE_DIR), "pi", "pi_sensor_agent.py"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return os.path.abspath(p)
    return os.path.abspath(candidates[0])

# Default output directory for CSV/video recordings. Overridable via
# SENSOR_DASHBOARD_DATA_DIR (e.g. to keep recordings outside the repo).
DEFAULT_DATA_DIR = os.environ.get("SENSOR_DASHBOARD_DATA_DIR") or os.path.join(REPO_ROOT, "experiment_data")
AGENT_PATH = _find_agent_path()

# --- Global State ---
discovery_map = {}
latest_sensor_data = {}
recording_state = {
    "is_recording": False,
    "start_time": 0,
    "session_timestamp": "",
    "sensor_recorder": None,   # CsvRecorder (sensor values)
    "marker_recorder": None,   # CsvRecorder (AprilTag poses)
    "save_dir": DEFAULT_DATA_DIR,
    "prefix": ""
}
asyncio_loop = None

# --- Device Discovery ---
class PiScanner:
    def __init__(self, callback):
        self.callback = callback
        self.zeroconf = Zeroconf()
        self.browser = ServiceBrowser(self.zeroconf, ["_ssh._tcp.local.", "_workstation._tcp.local."], self)
        self.found_devices = {}
    def add_service(self, zeroconf, type, name):
        info = zeroconf.get_service_info(type, name, 3000)
        if info and info.addresses:
            host = name.split(".")[0] + ".local"
            ip = socket.inet_ntoa(info.addresses[0])
            self.found_devices[host] = ip
            self.callback(self.found_devices)
    def update_service(self, zeroconf, type, name): self.add_service(zeroconf, type, name)
    def remove_service(self, *args): pass

# --- WebSocket Manager ---
class ConnectionManager:
    def __init__(self): self.active_connections = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket): self.active_connections.remove(websocket)
    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try: await connection.send_json(message)
            except: pass

manager = ConnectionManager()

def get_hostname_from_ip(ip):
    for host, found_ip in discovery_map.items():
        if found_ip == ip: return host
    return "Unknown Pi"

def on_discovery(found_dict):
    global discovery_map
    discovery_map = found_dict
    _broadcast_threadsafe({"type": "discovery", "devices": discovery_map})

# --- TCP Server ---
def _broadcast_threadsafe(message: dict):
    """Safely hand a broadcast off from a TCP/zeroconf thread to the event loop."""
    if asyncio_loop:
        asyncio.run_coroutine_threadsafe(manager.broadcast(message), asyncio_loop)

def run_tcp_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((TCP_HOST, TCP_PORT))
        s.listen(10)
        while True:
            conn, addr = s.accept()
            if not _pi_address_allowed(addr[0]):
                logger.warning("Refused sensor connection from %s: not in SENSOR_DASHBOARD_PI_ALLOWED_IPS", addr[0])
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=handle_pi_client, args=(conn, addr), daemon=True).start()


def _pi_address_allowed(ip: str, nets=None) -> bool:
    """Whether a sensor agent at ``ip`` may connect (always True with no allow-list)."""
    nets = PI_ALLOWED_NETS if nets is None else nets
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in nets)


class InvalidSensorMessage(ValueError):
    """A sensor_data line that fails type checking; it is logged and dropped."""


SENSOR_NUMBER_FIELDS = ("temp", "humi", "lux_raw", "uv", "voc")
SENSOR_VECTOR_FIELDS = ("accel", "gyro")
SENSOR_NAMES = ("bme280", "tsl25911", "icm20948", "ltr390", "sgp40")


def _number_or_none(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSensorMessage(f"{field} must be a number or null, got {type(value).__name__}")
    if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
        raise InvalidSensorMessage(f"{field} must be finite")
    return value


def validate_sensor_message(msg) -> dict:
    """Type-check one decoded sensor line and return a clean copy.

    Only known fields are kept, so nothing a Pi (or anyone on the LAN) sends
    beyond them reaches latest_sensor_data, the browser or the CSV. Every
    reading may be ``null`` (the agent sends null for a failed read); the
    vectors must be ``null`` or a list of exactly three numbers-or-null. The
    legacy key ``lux`` of pre-2026-09-23 agents (which was the same raw
    channel-0 count) is accepted as ``lux_raw``. Raises InvalidSensorMessage.
    """
    if not isinstance(msg, dict):
        raise InvalidSensorMessage(f"message must be a JSON object, got {type(msg).__name__}")
    if msg.get("type") != "sensor_data":
        raise InvalidSensorMessage(f"unsupported message type {msg.get('type')!r}")
    raw = dict(msg)
    if "lux_raw" not in raw and "lux" in raw:
        raw["lux_raw"] = raw["lux"]
    clean = {"type": "sensor_data"}
    for field in SENSOR_NUMBER_FIELDS:
        clean[field] = _number_or_none(raw.get(field), field)
    for field in SENSOR_VECTOR_FIELDS:
        vec = raw.get(field)
        if vec is None:
            clean[field] = None
            continue
        if not isinstance(vec, (list, tuple)) or len(vec) != 3:
            raise InvalidSensorMessage(f"{field} must be null or a list of 3 numbers")
        clean[field] = [_number_or_none(v, f"{field}[{i}]") for i, v in enumerate(vec)]
    ok = raw.get("ok")
    if ok is None:
        # Older agents send no flags: infer them from which values are present.
        ok = {
            "bme280": clean["temp"] is not None,
            "tsl25911": clean["lux_raw"] is not None,
            "icm20948": clean["accel"] is not None,
            "ltr390": clean["uv"] is not None,
            "sgp40": clean["voc"] is not None,
        }
    elif not isinstance(ok, dict) or not all(isinstance(v, bool) for v in ok.values()):
        raise InvalidSensorMessage("ok must be a mapping of sensor name to true/false")
    clean["ok"] = {name: bool(ok.get(name, False)) for name in SENSOR_NAMES}
    model = raw.get("model")
    clean["model"] = model[:64] if isinstance(model, str) else None
    return clean


def _read_auth_line(conn, ip: str) -> tuple[bool, str]:
    """Read the first line of a connection and check it against PI_TOKEN.

    Returns ``(ok, leftover)`` where leftover is whatever arrived after the
    auth line (the first sensor lines, if the agent sent them in one packet).
    """
    buffer = ""
    conn.settimeout(PI_AUTH_TIMEOUT_S)
    try:
        while "\n" not in buffer:
            data = conn.recv(4096)
            if not data:
                return False, ""
            buffer += data.decode(errors="ignore")
            if len(buffer) > TCP_MAX_LINE:
                return False, ""
    except (socket.timeout, OSError):
        return False, ""
    finally:
        try:
            conn.settimeout(None)
        except OSError:
            pass
    line, rest = buffer.split("\n", 1)
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return False, ""
    if not (isinstance(msg, dict) and msg.get("type") == "auth"
            and isinstance(msg.get("token"), str)
            and secrets.compare_digest(msg["token"], PI_TOKEN)):
        return False, ""
    return True, rest


def _ingest_line(line: str, ip: str, hostname: str) -> str:
    """Handle one received line. Returns the (possibly refreshed) hostname.

    Any problem with the line is logged and the line dropped; nothing raised
    here can end the connection or the recording thread.
    """
    if len(line) > TCP_MAX_LINE or not line.strip():
        return hostname
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("Dropped non-JSON line from %s", ip)
        return hostname
    if isinstance(msg, dict) and msg.get("type") == "auth":
        return hostname  # a repeated auth line (or one sent when no token is configured)
    try:
        clean = validate_sensor_message(msg)
    except InvalidSensorMessage as e:
        logger.warning("Dropped malformed sensor message from %s: %s", ip, e)
        return hostname
    if hostname == "Unknown Pi":
        hostname = get_hostname_from_ip(ip)
    clean["ip"] = ip
    clean["hostname"] = hostname
    # Arrival time of this sample; recording_loop writes it next to its own
    # tick time, so a stale value (a Pi that stopped sending) is visible.
    clean["received_at"] = datetime.now(timezone.utc).isoformat()
    latest_sensor_data[ip] = clean
    _broadcast_threadsafe(clean)
    return hostname


def handle_pi_client(conn, addr):
    ip = addr[0]
    hostname = get_hostname_from_ip(ip)
    buffer = ""
    announced = False
    try:
        with conn:
            if PI_TOKEN:
                ok, buffer = _read_auth_line(conn, ip)
                if not ok:
                    logger.warning("Refused sensor connection from %s: missing or wrong SENSOR_DASHBOARD_PI_TOKEN", ip)
                    return
            _broadcast_threadsafe({"type": "connect", "ip": ip, "hostname": hostname})
            announced = True
            while True:
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    try:
                        hostname = _ingest_line(line, ip, hostname)
                    except Exception:
                        logger.exception("Unexpected error handling a line from %s; line dropped", ip)
                data = conn.recv(4096).decode(errors="ignore")
                if not data: break
                buffer += data
                # cut off input that would otherwise grow unbounded with no newline (DoS protection)
                if len(buffer) > TCP_MAX_BUFFER:
                    logger.warning("TCP buffer overflow from %s; closing connection", ip)
                    break
    except OSError as e:
        logger.info("Sensor connection from %s ended: %s", ip, e)
    finally:
        if announced:
            latest_sensor_data.pop(ip, None)
            _broadcast_threadsafe({"type": "disconnect", "ip": ip})

# --- Recording (append-as-you-go) ---
class CsvRecorder:
    """Thread-safe, append-as-you-go CSV recorder.

    The older approach of buffering every row in RAM and writing it all at
    stop time exhausts memory on long recordings and loses everything if the
    process crashes mid-session. This class instead appends each row as it
    arrives and flushes to disk every FLUSH_INTERVAL seconds. The file is
    created only once the first row arrives (if no data ever arrives, no file
    is created — preserving the previous behavior).
    """

    FLUSH_INTERVAL = 1.0  # seconds

    def __init__(self, filepath: str, fieldnames: list):
        self.filepath = filepath
        self.fieldnames = fieldnames
        self.rows_written = 0
        self._lock = threading.Lock()
        self._file = None
        self._writer = None
        self._closed = False
        self._failed = False
        self._last_flush = 0.0

    def write_row(self, row: dict):
        with self._lock:
            if self._closed or self._failed:
                return
            try:
                if self._file is None:
                    os.makedirs(os.path.dirname(self.filepath) or ".", exist_ok=True)
                    self._file = open(self.filepath, "w", newline="", encoding="utf-8")
                    self._writer = csv.DictWriter(self._file, fieldnames=self.fieldnames)
                    self._writer.writeheader()
                self._writer.writerow(row)
                self.rows_written += 1
                now = time.monotonic()
                if now - self._last_flush >= self.FLUSH_INTERVAL:
                    self._file.flush()
                    self._last_flush = now
            except Exception as e:
                # a write failure (e.g. disk full) must not take down the recording thread
                self._failed = True
                logger.error("Failed to write CSV (%s): %s", self.filepath, e)

    def close(self):
        """Flush and close. Returns the path if at least one row was written, else None."""
        with self._lock:
            self._closed = True
            if self._file is not None:
                try:
                    self._file.flush()
                    os.fsync(self._file.fileno())
                    self._file.close()
                except Exception as e:
                    logger.error("Failed to close CSV (%s): %s", self.filepath, e)
                self._file = None
                self._writer = None
            return self.filepath if self.rows_written else None


def _sensor_row(ip, msg, tick_iso):
    """One sensor-CSV row from a validated message. None values become blank cells."""
    acc = msg.get("accel") or [None, None, None]
    gyr = msg.get("gyro") or [None, None, None]
    return {
        "timestamp": tick_iso,
        "received_at": msg.get("received_at"),
        "hostname": msg.get("hostname") or get_hostname_from_ip(ip),
        "temperature": msg.get("temp"),
        "humidity": msg.get("humi"),
        "lux_raw": msg.get("lux_raw"),
        "uvi": msg.get("uv"),
        "voc_raw": msg.get("voc"),
        "acc_x": acc[0], "acc_y": acc[1], "acc_z": acc[2],
        "gyro_x": gyr[0], "gyro_y": gyr[1], "gyro_z": gyr[2],
    }


def recording_loop():
    """Write one row per connected Pi every 100 ms while recording.

    ``timestamp`` is this tick's time, ``received_at`` the arrival time of
    the sample being written (the latest one from that Pi), so a Pi that
    stopped sending shows up as a growing gap between the two columns.
    A bad entry is logged and skipped; it never ends the loop.
    """
    while recording_state["is_recording"]:
        now_iso = datetime.now(timezone.utc).isoformat()
        recorder = recording_state.get("sensor_recorder")
        if recorder is not None:
            for ip, msg in list(latest_sensor_data.items()):
                try:
                    recorder.write_row(_sensor_row(ip, msg, now_iso))
                except Exception:
                    logger.exception("Skipped a sensor row for %s", ip)
        time.sleep(0.1)

CSV_FIELDS = [
    "timestamp", "received_at", "hostname",
    "temperature", "humidity", "lux_raw", "uvi", "voc_raw",
    "acc_x", "acc_y", "acc_z",
    "gyro_x", "gyro_y", "gyro_z",
]

MARKER_CSV_FIELDS = [
    "timestamp", "tag_id",
    "tx_mm", "ty_mm", "tz_mm",
    "roll", "pitch", "yaw",
    "dx_mm", "dy_mm", "dz_mm",
    "droll", "dpitch", "dyaw",
    "outlier",
]


def begin_recording_session():
    """Start a recording session (prefix/save_dir must already be set).

    The output path is fixed at the save_dir/prefix in effect when the
    session starts (an update_config call during recording only takes effect
    for the next session).
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    prefix = recording_state["prefix"]
    save_dir = recording_state["save_dir"]
    recording_state.update({
        "is_recording": True,
        "start_time": time.time(),
        "session_timestamp": timestamp,
        "sensor_recorder": CsvRecorder(
            os.path.join(save_dir, f"{prefix}session_{timestamp}.csv"), CSV_FIELDS),
        "marker_recorder": CsvRecorder(
            os.path.join(save_dir, f"{prefix}session_{timestamp}_tags.csv"), MARKER_CSV_FIELDS),
    })
    threading.Thread(target=recording_loop, daemon=True).start()


def finalize_recording_session():
    """Close the recorders and return the saved sensor-CSV path (None if no row was written)."""
    sensor = recording_state.get("sensor_recorder")
    marker = recording_state.get("marker_recorder")
    recording_state["sensor_recorder"] = None
    recording_state["marker_recorder"] = None
    saved_path = sensor.close() if sensor else None
    marker_path = marker.close() if marker else None
    if saved_path:
        logger.info("Saved sensor CSV: %s (%d rows)", saved_path, sensor.rows_written)
    if marker_path:
        logger.info("Saved marker CSV: %s (%d rows)", marker_path, marker.rows_written)
    return saved_path

# --- Lifespan Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global asyncio_loop
    asyncio_loop = asyncio.get_event_loop()
    threading.Thread(target=run_tcp_server, daemon=True).start()
    PiScanner(on_discovery)
    if os.environ.get("SENSOR_DASHBOARD_HEADLESS", "0") != "1":
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(f"http://localhost:{WEB_PORT}")), daemon=True).start()
    yield

app = FastAPI(lifespan=lifespan)
# Host header allow-list (DNS-rebinding protection). No wildcards are used.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

# Privileged operations that must never run from anything but the local console.
PRIVILEGED_ACTIONS = {"update_config", "pick_folder", "open_folder"}
# Recording control over /ws. When SENSOR_DASHBOARD_TOKEN is set these need a
# matching token (same semantics as POST /api/start and /api/end); when no token
# is configured — the documented localhost-only default — they are restricted to
# local clients, exactly like PRIVILEGED_ACTIONS.
RECORDING_ACTIONS = {"start_recording", "stop_recording"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_host = websocket.client.host if websocket.client else ""
    origin = websocket.headers.get("origin")
    if not _origin_allowed(origin, client_host):
        logger.warning(
            "Rejected WebSocket connection from client %s with Origin %r (allowed hosts: %s)",
            client_host or "?", origin, ALLOWED_HOSTS,
        )
        # Accept first, then close with 1008 (policy violation), so the client
        # gets a WebSocket-level reason rather than an opaque handshake failure.
        # Nothing is read from the socket and it is never registered with the
        # broadcast manager, so no command from this peer is ever executed.
        await websocket.accept()
        await websocket.close(code=1008)
        return
    # Token supplied at connect time (?token=...); individual messages may also
    # carry a "token" field. Either is accepted for the recording actions.
    conn_token = websocket.query_params.get("token")
    await manager.connect(websocket)
    await websocket.send_json({"type": "config_update", "save_dir": recording_state["save_dir"], "prefix": recording_state["prefix"]})
    await websocket.send_json({"type": "discovery", "devices": discovery_map})
    await websocket.send_json({
        "type": "recording_status",
        "is_recording": recording_state["is_recording"],
        "session_timestamp": recording_state["session_timestamp"],
        "prefix": recording_state["prefix"],
    })
    try:
        while True:
            cmd = await websocket.receive_json()
            await handle_web_command(cmd, client_host, websocket, conn_token)
    except WebSocketDisconnect: manager.disconnect(websocket)

async def handle_web_command(cmd, client_host: str = "", websocket: WebSocket | None = None, conn_token: str | None = None):
    global recording_state
    action = cmd.get("action")

    # privileged operations are only allowed for connections from the local machine
    if action in PRIVILEGED_ACTIONS and not _is_loopback(client_host):
        logger.warning("Rejected privileged action '%s' from non-local client %s", action, client_host)
        if websocket is not None:
            await websocket.send_json({"type": "error", "action": action, "detail": "privileged action allowed only from localhost"})
        return

    if action in RECORDING_ACTIONS:
        if AUTH_TOKEN:
            if not (_token_ok(cmd.get("token")) or _token_ok(conn_token)):
                logger.warning("Rejected '%s' from %s: invalid or missing auth token", action, client_host or "?")
                if websocket is not None:
                    await websocket.send_json({"type": "error", "action": action, "detail": "invalid or missing auth token"})
                return
        elif not _is_loopback(client_host):
            logger.warning("Rejected '%s' from non-local client %s (no SENSOR_DASHBOARD_TOKEN configured)", action, client_host)
            if websocket is not None:
                await websocket.send_json({"type": "error", "action": action, "detail": "set SENSOR_DASHBOARD_TOKEN to control recording from a non-local client"})
            return

    if action == "start_recording":
        if recording_state["is_recording"]:
            return  # prevent a double start (which would spawn multiple recording threads)
        begin_recording_session()
        await manager.broadcast({
            "type": "recording_status",
            "is_recording": True,
            "session_timestamp": recording_state["session_timestamp"],
            "prefix": recording_state["prefix"],
        })
    elif action == "stop_recording":
        if not recording_state["is_recording"]:
            return
        recording_state["is_recording"] = False
        await asyncio.sleep(0.2)  # wait for the recording thread's last tick
        saved_path = await asyncio.to_thread(finalize_recording_session)
        await manager.broadcast({"type": "recording_status", "is_recording": False, "path": saved_path})
    elif action == "update_config":
        save_dir = cmd.get("save_dir")
        if save_dir:
            recording_state["save_dir"] = os.path.abspath(os.path.normpath(save_dir))
        recording_state["prefix"] = _sanitize_name(cmd.get("prefix", ""))
        await manager.broadcast({"type": "config_update", "save_dir": recording_state["save_dir"], "prefix": recording_state["prefix"]})
    elif action == "pick_folder":
        import tkinter as tk
        from tkinter import filedialog

        def select_dir():
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(initialdir=recording_state["save_dir"])
            root.destroy()
            return path

        # Run in thread to not block websocket
        selected_path = await asyncio.to_thread(select_dir)
        if selected_path:
            recording_state["save_dir"] = os.path.abspath(selected_path)
            await manager.broadcast({
                "type": "config_update",
                "save_dir": recording_state["save_dir"],
                "prefix": recording_state["prefix"]
            })
    elif action == "open_folder":
        # Arbitrary-path launching via os.startfile has been removed; only an
        # existing directory is reported back to the client.
        path = cmd.get("path") or recording_state["save_dir"]
        path = os.path.abspath(os.path.normpath(path))
        if os.path.isdir(path):
            await manager.broadcast({"type": "folder_path", "path": path})
        elif websocket is not None:
            await websocket.send_json({"type": "error", "action": action, "detail": "not a directory"})
    elif action == "deploy":
        # The remote-execution (shell=True) path has been removed. Use
        # src/monitoring/pi/install_pi_agent.ps1 to install the Pi agent.
        if websocket is not None:
            await websocket.send_json({
                "type": "error", "action": action,
                "detail": "remote deploy disabled; use src/monitoring/pi/install_pi_agent.ps1",
            })

# --- External HTTP API ---
class StartRequest(BaseModel):
    experiment_name: str | None = None

@app.get("/api/status")
async def api_status():
    return {
        "is_recording": recording_state["is_recording"],
        "device_count": len(latest_sensor_data),
        "devices": list(latest_sensor_data.keys()),
        "save_dir": recording_state["save_dir"],
    }

@app.get("/api/sensors")
async def api_sensors():
    return latest_sensor_data

@app.get("/api/sensors/{ip}")
async def api_sensor_one(ip: str):
    if ip not in latest_sensor_data:
        raise HTTPException(status_code=404, detail=f"No data for {ip}")
    return latest_sensor_data[ip]

@app.post("/api/start", dependencies=STATE_CHANGING)
async def api_start(req: StartRequest):
    if recording_state["is_recording"]:
        raise HTTPException(status_code=409, detail="Already recording")
    if req.experiment_name:
        safe = _sanitize_name(req.experiment_name)
        recording_state["prefix"] = f"{safe}_" if safe else ""
    begin_recording_session()
    await manager.broadcast({
        "type": "recording_status",
        "is_recording": True,
        "session_timestamp": recording_state["session_timestamp"],
        "prefix": recording_state["prefix"],
    })
    await manager.broadcast({
        "type": "config_update",
        "save_dir": recording_state["save_dir"],
        "prefix": recording_state["prefix"],
    })
    return {
        "status": "started",
        "experiment_name": req.experiment_name,
        "save_dir": recording_state["save_dir"],
    }

@app.get("/is_safe")
@app.get("/api/is_safe")
async def api_is_safe():
    return {"safe": True}

@app.post("/api/end", dependencies=STATE_CHANGING)
async def api_end():
    if not recording_state["is_recording"]:
        raise HTTPException(status_code=409, detail="No active session")
    recording_state["is_recording"] = False
    await asyncio.sleep(0.2)  # wait for the recording thread's last tick
    saved_path = await asyncio.to_thread(finalize_recording_session)
    await manager.broadcast({"type": "recording_status", "is_recording": False, "path": saved_path})
    return {"status": "ended", "saved_path": saved_path}

@app.post("/api/zero_tags", dependencies=STATE_CHANGING)
async def api_zero_tags():
    """Capture all currently-tracked markers' poses as zero references."""
    now = time.time()
    captured = []
    with _pose_lock:
        with _zero_lock:
            _zero_refs.clear()
            for tag_id, st in _pose_history.items():
                if now - st["t"] < 0.5:  # only zero markers seen in last 500ms
                    _zero_refs[tag_id] = {
                        "tvec": st["tvec"].copy(),
                        "R": st["R"].copy(),
                    }
                    captured.append(tag_id)
    return {"zeroed_ids": sorted(captured)}


@app.post("/api/clear_zero", dependencies=STATE_CHANGING)
async def api_clear_zero():
    with _zero_lock:
        _zero_refs.clear()
    return {"cleared": True}


@app.post("/api/detect_tags", dependencies=STATE_CHANGING)
async def api_detect_tags(
    image: UploadFile = File(...),
    tag_size_mm: float = Form(25.0),
    frame_w: int = Form(1280),
    frame_h: int = Form(720),
    smooth: int = Form(1),
):
    content = await image.read()
    if not content:
        return {"markers": []}
    tag_size_m = max(0.001, tag_size_mm / 1000.0)
    markers = await asyncio.to_thread(
        _detect_apriltags_sync, content, tag_size_m, frame_w, frame_h, bool(smooth)
    )
    recorder = recording_state.get("marker_recorder")
    if recording_state["is_recording"] and markers and recorder is not None:
        now_iso = datetime.now(timezone.utc).isoformat()
        for m in markers:
            d = m.get("delta") or {}
            recorder.write_row({
                "timestamp": now_iso,
                "tag_id": m["id"],
                "tx_mm": round(m["tx_mm"], 3),
                "ty_mm": round(m["ty_mm"], 3),
                "tz_mm": round(m["tz_mm"], 3),
                "roll": round(m["roll"], 3),
                "pitch": round(m["pitch"], 3),
                "yaw": round(m["yaw"], 3),
                "dx_mm": round(d["dx_mm"], 3) if d else "",
                "dy_mm": round(d["dy_mm"], 3) if d else "",
                "dz_mm": round(d["dz_mm"], 3) if d else "",
                "droll": round(d["droll"], 3) if d else "",
                "dpitch": round(d["dpitch"], 3) if d else "",
                "dyaw": round(d["dyaw"], 3) if d else "",
                "outlier": int(bool(m.get("outlier"))),
            })
    return {"markers": markers}


@app.post("/api/upload_video", dependencies=STATE_CHANGING)
async def api_upload_video(
    video: UploadFile = File(...),
    timestamp: str = Form(...),
    prefix: str = Form(""),
):
    safe_ts = "".join(c for c in timestamp if c.isalnum() or c in "_-")
    safe_prefix = "".join(c for c in prefix if c.isalnum() or c in "_-")
    save_dir = recording_state["save_dir"]
    os.makedirs(save_dir, exist_ok=True)
    filepath = os.path.join(save_dir, f"{safe_prefix}session_{safe_ts}.webm")
    written = 0
    try:
        with open(filepath, "wb") as f:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > UPLOAD_MAX_BYTES:
                    raise HTTPException(status_code=413, detail=f"video exceeds {UPLOAD_MAX_BYTES} bytes")
                f.write(chunk)
    except HTTPException:
        # Do not leave a truncated file behind when the limit is hit.
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise
    logger.info("Video saved to %s (%d bytes)", filepath, written)
    return {"status": "saved", "path": filepath}

# Static mount must come last — it catches "/" and below
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    if not _is_loopback(WEB_HOST) and WEB_HOST not in ("0.0.0.0", "::") and not AUTH_TOKEN:
        logger.warning(
            "Web server bound to %s without SENSOR_DASHBOARD_TOKEN set; "
            "recording-control API is unauthenticated on the LAN.", WEB_HOST,
        )
    if WEB_HOST in ("0.0.0.0", "::") and not AUTH_TOKEN:
        logger.warning(
            "Web server bound to all interfaces (%s) without SENSOR_DASHBOARD_TOKEN; "
            "set a token or use SENSOR_DASHBOARD_HOST=127.0.0.1 for local-only access.", WEB_HOST,
        )
    if not PI_ALLOWED_NETS and not PI_TOKEN:
        logger.warning(
            "Sensor listener %s:%d accepts data from any host on the network; set "
            "SENSOR_DASHBOARD_PI_ALLOWED_IPS and/or SENSOR_DASHBOARD_PI_TOKEN to restrict it.",
            TCP_HOST, TCP_PORT,
        )
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)
