"""microscope_resolution config: parsed into the driver, 4K by default."""
import asyncio

import pytest

from src import config as lab_config
from src.devices.safety.shared_devices import SharedDevices, parse_resolution


def test_parse_resolution():
    assert parse_resolution(None) is None
    assert parse_resolution("") is None
    assert parse_resolution("1280x720") == (1280, 720)
    assert parse_resolution("3840X2160") == (3840, 2160)
    assert parse_resolution("1920\u00d71080") == (1920, 1080)
    assert parse_resolution((640, 480)) == (640, 480)
    with pytest.raises(ValueError):
        parse_resolution("4k")


def test_default_config_is_4k():
    assert lab_config.DEFAULTS["shared_devices"]["microscope_resolution"] == "3840x2160"


def test_shared_devices_pass_resolution_to_driver(monkeypatch):
    import src.devices.microscope as pkg
    created = {}

    class FakeCamera:
        def __init__(self, camera_index, resolution=None):
            created["resolution"] = resolution

        def connect(self):
            return True

        def disconnect(self):
            pass

    monkeypatch.setattr(pkg, "MicroscopeController", FakeCamera)
    shared = SharedDevices(use_microscope=True, microscope_index=2, microscope_resolution="1280x720")
    assert asyncio.run(shared.initialize()) is True
    assert created["resolution"] == (1280, 720)
    shared = SharedDevices(use_microscope=True, microscope_index=2)
    assert asyncio.run(shared.initialize()) is True
    assert created["resolution"] is None, "no config -> driver default (4K)"
