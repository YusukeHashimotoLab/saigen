"""Shared directory layout for the imaging module.

All modules import their data locations from here, so every entry point --
`camera_server.py` (which launches `acquire.py` as a subprocess), a manual
`python acquire.py`, or `make_panel.py` -- agrees on one data root no matter
which directory it was started from. Locations are anchored to this file,
never to the current working directory; only `IMAGING_DATA_DIR` moves them.

    IMAGING_DATA_DIR  (env var)  root for captured photos and analysis output.
                                 Default: <repo root>/imaging_data, i.e. the
                                 directory two levels above this module
                                 (git-ignored, outside the source package).
    UVC_UTIL          (env var)  path to the `uvc-util` binary used for camera
                                 control on macOS. Default: `uvc-util` on PATH,
                                 falling back to <module dir>/bin/uvc-util.
    IMAGING_SERVER_URL (env var) base URL of the running camera_server.py that
                                 acquire.py talks to. camera_server.py sets it
                                 for the acquisition child it launches, so a
                                 server started with --port N is always found.
                                 Default: http://127.0.0.1:8799.
    NEEWER_DEVICE_ID  (env var or repo-root .env) optional identifier of the
                                 NEEWER light to use when more than one is in
                                 range (see neewer_light.py). Read with
                                 env_setting(), never written anywhere.

A relative IMAGING_DATA_DIR is resolved once, against the directory the
process was started from; camera_server.py passes the *resolved* absolute
path to the acquisition child (which runs with cwd = this module's
directory), so both processes always agree on one data root.
"""
import os
import shutil
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent.parent            # <repo>/src/imaging -> <repo>
CONFIG_DIR = MODULE_DIR / "config"
STATIC_DIR = MODULE_DIR / "static"
EXAMPLES_DIR = MODULE_DIR / "examples"

DATA_DIR = Path(os.environ.get("IMAGING_DATA_DIR", REPO_ROOT / "imaging_data")).resolve()
PHOTOS_DIR = DATA_DIR / "photos"       # raw captures from the camera server
OUT_DIR = DATA_DIR / "analysis"        # per-run analysis output (spectral_<timestamp>/)

CAMERA_REFERENCE = CONFIG_DIR / "camera_reference.json"
CAMERA_LOCAL = CONFIG_DIR / "camera_local.json"       # per-PC camera selection (git-ignored)
LIGHT_REFERENCE = CONFIG_DIR / "light_reference.json"

# Measurement-sequence lock (held by acquire.py for a whole run) and the
# owner token the running acquisition writes next to it. While the lock is
# held, camera_server.py accepts /api/control writes only when they carry
# this token (header RUN_TOKEN_HEADER), so the Camera tab cannot change a
# setting in the middle of a run.
SPECTRAL_LOCK = OUT_DIR / ".spectral.lock"
RUN_OWNER_PATH = OUT_DIR / ".spectral.owner"
RUN_TOKEN_HEADER = "X-Imaging-Run-Token"

DEFAULT_SERVER_URL = "http://127.0.0.1:8799"


def server_url() -> str:
    """Base URL of the camera server (IMAGING_SERVER_URL, else the default)."""
    return (os.environ.get("IMAGING_SERVER_URL") or DEFAULT_SERVER_URL).rstrip("/")


def env_setting(name: str, env_file: Path | None = None) -> str | None:
    """Return a setting from the environment, else from the repo-root `.env`.

    `.env` is parsed read-only (python-dotenv if installed, otherwise a
    minimal KEY=VALUE reader); nothing is exported into os.environ. Empty
    values count as unset.
    """
    v = os.environ.get(name)
    if v and v.strip():
        return v.strip()
    env_file = REPO_ROOT / ".env" if env_file is None else env_file
    if not env_file.is_file():
        return None
    try:
        from dotenv import dotenv_values
        v = dotenv_values(env_file).get(name)
    except ImportError:
        v = None
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip().removeprefix("export ").strip() == name:
                v = val.split(" #")[0].strip().strip("'\"")
    return v.strip() if v and v.strip() else None


def uvc_util_path() -> str:
    """Return the `uvc-util` executable to use on macOS (not bundled; see README)."""
    env = os.environ.get("UVC_UTIL")
    if env:
        return env
    found = shutil.which("uvc-util")
    if found:
        return found
    return str(MODULE_DIR / "bin" / "uvc-util")


def resolve_input(path) -> Path:
    """Resolve a user-supplied input path (a run directory, typically).

    Tried in order: as given (absolute, or relative to the current working
    directory), then relative to this module's directory, then relative to
    the data root. This is what lets the same command line work from
    `src/imaging` and from the repository root, e.g.

        python make_panel.py examples/zif8_vial_5s
        python src/imaging/make_panel.py examples/zif8_vial_5s

    The path is returned unchanged (merely absolute) if none of the
    candidates exists, so the caller can report the name the user typed.
    """
    p = Path(path)
    if p.is_absolute():
        return p
    for base in (Path.cwd(), MODULE_DIR, DATA_DIR):
        cand = base / p
        if cand.exists():
            return cand
    return (Path.cwd() / p)


def ensure_dirs() -> None:
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
