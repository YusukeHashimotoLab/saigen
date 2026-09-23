"""Loader for the environment-dependent settings of the control core.

``config.yaml`` in the repository root is the single source of truth for the
values that change between installations: COM ports, camera indices and the
Dobot workspace limits.

Resolution order (first file that exists wins, merged over ``DEFAULTS``):

    1. ``<repo>/config.yaml``          - your own copy, gitignored
    2. ``<repo>/config.example.yaml``  - tracked template, same values as DEFAULTS
    3. ``DEFAULTS`` below              - used when PyYAML is missing or both files are

Missing keys are filled in from ``DEFAULTS``, so a partial config.yaml that only
overrides a couple of ports keeps working after an upgrade.

Sensor-dashboard ports and the video-recording camera index are NOT duplicated
here: they belong to src/monitoring and are read from
``src/monitoring/config.yaml`` (falling back to ``config.example.yaml`` there),
exactly as launch_sensor_dashboard.py does.
"""
import copy
import logging
import os

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")
EXAMPLE_CONFIG_PATH = os.path.join(REPO_ROOT, "config.example.yaml")

MONITORING_DIR = os.path.join(REPO_ROOT, "src", "monitoring")

# Fallback used when no YAML file can be read. Keep in sync with
# config.example.yaml (tests/test_config.py checks for drift).
DEFAULTS = {
    "robots": {
        1: {"dobot_port": "COM3", "picus2_address": "COM4"},
        2: {"dobot_port": "COM5", "picus2_address": "COM6"},
        3: {"dobot_port": "COM7"},
    },
    "shared_devices": {
        "scale_port": "COM8",
        "camera_index": 0,
        "microscope_index": 2,
        "microscope_port": "",
        "microscope_resolution": "3840x2160",
    },
    "workspace": {
        "x_min": -300.0, "x_max": 300.0,
        "y_min": -300.0, "y_max": 300.0,
        "z_min": -130.0, "z_max": 150.0,
        "joint1_min": -135.0, "joint1_max": 135.0,
    },
}

# Same defaults launch_sensor_dashboard.py falls back to.
MONITORING_DEFAULTS = {
    "sensor_dashboard": {"web_port": 8000, "tcp_port": 50001},
    "video": {"camera_index": 1},
}

_config_cache = None
_monitoring_cache = None


def _deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: ``base`` recursively overridden by ``override``."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml_file(path: str):
    """Read one YAML mapping, or return None if unusable (never raises)."""
    try:
        import yaml
    except ImportError:
        logger.warning(
            "PyYAML is not installed, so %s cannot be read; using built-in "
            "defaults (run: pip install -r requirements.txt)", path
        )
        return None
    try:
        with open(path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            raise ValueError("the top level must be a mapping")
        return loaded
    except Exception as e:  # noqa: BLE001 - config must never break a run
        logger.error("Could not read %s; using built-in defaults: %s", path, e)
        return None


def load_config(force_reload: bool = False) -> dict:
    """Return config.yaml (or config.example.yaml) merged over DEFAULTS."""
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache

    config = copy.deepcopy(DEFAULTS)
    for path in (CONFIG_PATH, EXAMPLE_CONFIG_PATH):
        if not os.path.exists(path):
            continue
        loaded = _load_yaml_file(path)
        if loaded is not None:
            config = _deep_merge(config, loaded)
        break
    else:
        logger.warning(
            "Neither %s nor %s found; using built-in defaults",
            CONFIG_PATH, EXAMPLE_CONFIG_PATH,
        )

    _config_cache = config
    return config


def load_monitoring_config(force_reload: bool = False) -> dict:
    """Return src/monitoring/config.yaml merged over MONITORING_DEFAULTS."""
    global _monitoring_cache
    if _monitoring_cache is not None and not force_reload:
        return _monitoring_cache

    config = copy.deepcopy(MONITORING_DEFAULTS)
    for name in ("config.yaml", "config.example.yaml"):
        path = os.path.join(MONITORING_DIR, name)
        if not os.path.exists(path):
            continue
        loaded = _load_yaml_file(path)
        if loaded is not None:
            config = _deep_merge(config, loaded)
        break

    _monitoring_cache = config
    return config


def get_robot_ports() -> dict:
    """robot_id (int) -> connection ports for that robot."""
    return {int(rid): dict(ports) for rid, ports in load_config()["robots"].items()}


def get_shared_devices() -> dict:
    """Settings for the shared devices (scale_port, camera_index, microscope_index)."""
    return dict(load_config()["shared_devices"])


def get_workspace() -> dict:
    """Dobot workspace limits (keys match the WorkspaceValidator arguments)."""
    return dict(load_config()["workspace"])


def get_video_camera_index() -> int:
    """OpenCV index of the camera used for video recording (src/monitoring)."""
    return int(load_monitoring_config()["video"]["camera_index"])


def get_dashboard_ports() -> dict:
    """Sensor-dashboard ports (web_port, tcp_port) from src/monitoring."""
    return {k: int(v) for k, v in load_monitoring_config()["sensor_dashboard"].items()}
