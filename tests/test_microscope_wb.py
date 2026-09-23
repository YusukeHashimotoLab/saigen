"""Microscope camera: white balance is written on connect (cures the power-up
green cast), and the shared-device focus keeps the video stream pumped."""
import asyncio
import sys

import cv2
import pytest

from src.devices.microscope import microscope_controller as mc
from src.devices.microscope.microscope_controller import MicroscopeController
from src.devices.safety.shared_devices import SharedDevices


class FakeCapture:
    instances = []

    def __init__(self, index, backend):
        self.index = index
        self.backend = backend
        self.props = {}
        self.opened = backend != "fail"
        self.released = False
        FakeCapture.instances.append(self)

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def get(self, prop):
        return self.props.get(prop, 0)

    def read(self):
        return True, None

    def release(self):
        self.released = True


def test_connect_uses_dshow_and_writes_white_balance(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mc.platform, "system", lambda: "Windows")
    monkeypatch.setattr(mc.cv2, "VideoCapture", FakeCapture)
    FakeCapture.instances.clear()
    scope = MicroscopeController(camera_index=2)
    assert scope.connect() is True
    cam = FakeCapture.instances[-1]
    assert cam.backend == cv2.CAP_DSHOW and scope.backend_name == "DSHOW"
    assert cam.props[cv2.CAP_PROP_AUTO_WB] == 0
    assert cam.props[cv2.CAP_PROP_WHITE_BALANCE_BLUE_U] == 5000.0
    assert cam.props[cv2.CAP_PROP_FRAME_WIDTH] == 3840 and cam.props[cv2.CAP_PROP_FRAME_HEIGHT] == 2160


def test_connect_falls_back_to_msmf(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mc.platform, "system", lambda: "Windows")

    class DshowFails(FakeCapture):
        def __init__(self, index, backend):
            super().__init__(index, "fail" if backend == cv2.CAP_DSHOW else backend)

    monkeypatch.setattr(mc.cv2, "VideoCapture", DshowFails)
    FakeCapture.instances.clear()
    scope = MicroscopeController(camera_index=2, white_balance=None)
    assert scope.connect() is True
    assert scope.backend_name == "MSMF"
    assert cv2.CAP_PROP_WHITE_BALANCE_BLUE_U not in FakeCapture.instances[-1].props


def test_connect_fails_when_no_backend_opens(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mc.platform, "system", lambda: "Windows")
    monkeypatch.setattr(mc.cv2, "VideoCapture", lambda i, b: FakeCapture(i, "fail"))
    scope = MicroscopeController(camera_index=9)
    assert scope.connect() is False
    assert scope.is_connected is False


# ---------------------------------------------------------------- frame pump
class CountingCamera:
    def __init__(self):
        self.reads = 0

    def read(self):
        self.reads += 1
        return True, None


class FakeMicroscope:
    def __init__(self, camera_index, resolution=None):
        self.camera = CountingCamera()

    def connect(self):
        return True

    def disconnect(self):
        pass


class SlowSerial:
    def __init__(self, port):
        pass

    def connect(self):
        return True

    def get_model(self):
        return "UM22TW0100"

    def disconnect(self):
        pass

    def autofocus(self, timeout_s):
        import time
        time.sleep(0.5)          # the motor "moves" for half a second
        return 1600, True


def test_focus_keeps_reading_frames_while_motor_moves(monkeypatch):
    import src.devices.microscope as pkg
    monkeypatch.setattr(pkg, "MicroscopeController", FakeMicroscope)
    monkeypatch.setattr(pkg, "UM22SerialController", SlowSerial)
    shared = SharedDevices(use_microscope=True, microscope_index=2, microscope_port="COM77")
    assert asyncio.run(shared.initialize()) is True
    result = asyncio.run(shared.focus_microscope("auto", timeout=5))
    assert result == {"focus_position": 1600, "focus_converged": True}
    assert shared.microscope.camera.reads >= 3, "frames must be pumped during the focus operation"
