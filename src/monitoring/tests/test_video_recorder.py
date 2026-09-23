"""VideoRecorder lifecycle with a fake OpenCV capture/writer (no camera needed)."""
import threading

import pytest

from src.monitoring.camera import video_recorder as vr


class FakeCap:
    def __init__(self, block=None):
        self.block = block          # an Event the read() waits on, to simulate a hang
        self.released = False
        self.reading = threading.Event()

    def isOpened(self):
        return True

    def set(self, *a):
        return True

    def get(self, prop):
        return 0

    def read(self):
        self.reading.set()
        if self.block is not None:
            self.block.wait(10)
        return True, object()

    def release(self):
        self.released = True


class FakeWriter:
    def __init__(self, *a, **k):
        self.released = False
        self.frames = 0

    def isOpened(self):
        return True

    def write(self, frame):
        assert not self.released, "write after release"
        self.frames += 1

    def release(self):
        self.released = True


@pytest.fixture
def fake_cv2(monkeypatch):
    made = {}

    def cap_factory(*a, **k):
        return made["cap"]

    def writer_factory(*a, **k):
        made["writer"] = FakeWriter()
        return made["writer"]

    monkeypatch.setattr(vr.cv2, "VideoCapture", cap_factory)
    monkeypatch.setattr(vr.cv2, "VideoWriter", writer_factory)
    return made


def test_clean_stop_releases_via_worker(fake_cv2, tmp_path):
    fake_cv2["cap"] = FakeCap()
    rec = vr.VideoRecorder(str(tmp_path / "v.mp4"), camera_index=0, fps=200)
    assert rec.start()
    fake_cv2["cap"].reading.wait(2)
    assert rec.stop() == str(tmp_path / "v.mp4")
    assert rec.last_stop_clean is True
    assert fake_cv2["cap"].released and fake_cv2["writer"].released


def test_hung_worker_is_not_released_under_it(fake_cv2, tmp_path):
    gate = threading.Event()
    fake_cv2["cap"] = FakeCap(block=gate)
    rec = vr.VideoRecorder(str(tmp_path / "v.mp4"), camera_index=0)
    assert rec.start()
    fake_cv2["cap"].reading.wait(2)
    worker = rec._thread
    path = rec.stop(timeout=0.1)
    assert path == str(tmp_path / "v.mp4")
    assert rec.last_stop_clean is False
    # stop() must not have released anything the worker is still using
    assert not fake_cv2["cap"].released and not fake_cv2["writer"].released
    gate.set()                      # the camera "comes back"
    worker.join(2)
    assert not worker.is_alive()
    assert fake_cv2["cap"].released and fake_cv2["writer"].released


def test_restart_after_unclean_stop_does_not_revive_old_worker(fake_cv2, tmp_path):
    gate = threading.Event()
    fake_cv2["cap"] = FakeCap(block=gate)
    rec = vr.VideoRecorder(str(tmp_path / "a.mp4"), camera_index=0)
    rec.start()
    fake_cv2["cap"].reading.wait(2)
    old_worker = rec._thread
    rec.stop(timeout=0.1)
    fake_cv2["cap"] = FakeCap()
    rec.output_path = str(tmp_path / "b.mp4")
    assert rec.start()
    gate.set()
    old_worker.join(2)
    assert not old_worker.is_alive()   # its own stop event stayed set
    rec.stop()
    assert rec.last_stop_clean is True


def test_stop_without_start_returns_none():
    assert vr.VideoRecorder("x.mp4").stop() is None
