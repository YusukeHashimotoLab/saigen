"""Run provenance: execution mode in every artefact, unique run folders,
sensor-recording ownership, and failed captures recorded as failures."""
import asyncio
import csv
import json
import os

import pytest

from src.flow import experiment_logger as exp_mod
from src.flow import run_flow
from src.flow.executor import execute_step
from src.flow.experiment_logger import ExperimentLogger


@pytest.fixture
def src_file(tmp_path):
    p = tmp_path / "flow.json"
    p.write_text("{}", encoding="utf-8")
    return str(p)


def _finish(log, status="completed"):
    log.finalize(status)
    with open(log.metadata_path, encoding="utf-8") as f:
        meta = json.load(f)
    with open(log.csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with open(log.summary_path, encoding="utf-8") as f:
        summary = f.read()
    return meta, rows, summary


@pytest.mark.parametrize("mode, label", [("mock", "MOCK"), ("real", "REAL")])
def test_mode_is_in_metadata_csv_and_summary(tmp_path, src_file, mode, label):
    log = ExperimentLogger("m", "d", src_file, base_dir=str(tmp_path / "logs"), mode=mode)
    log.record_step(1, 1, "go_home", 1, None, "ok", 0.1)
    meta, rows, summary = _finish(log)
    assert meta["mode"] == mode
    assert rows[0]["mode"] == mode
    assert f"**実行モード**: {label}" in summary


def test_mode_accepts_the_mock_flag_and_rejects_nonsense(tmp_path, src_file):
    assert ExperimentLogger("a", "", src_file, base_dir=str(tmp_path), mode=True).mode == "mock"
    with pytest.raises(ValueError):
        ExperimentLogger("b", "", src_file, base_dir=str(tmp_path), mode="simulated")


def test_same_second_runs_get_separate_folders(tmp_path, src_file, monkeypatch):
    class FrozenDatetime(exp_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 23, 12, 0, 0)
    monkeypatch.setattr(exp_mod, "datetime", FrozenDatetime)
    base = str(tmp_path / "logs")
    a = ExperimentLogger("same", "", src_file, base_dir=base, mode="mock")
    b = ExperimentLogger("same", "", src_file, base_dir=base, mode="mock")
    c = ExperimentLogger("same", "", src_file, base_dir=base, mode="mock")
    try:
        assert len({a.dir, b.dir, c.dir}) == 3
        assert b.dir == a.dir + "_2" and c.dir == a.dir + "_3"
        assert all(os.path.isdir(os.path.join(d, "images")) for d in (a.dir, b.dir, c.dir))
    finally:
        for log in (a, b, c):
            log.finalize("completed")


def test_run_folder_creation_does_not_use_exist_ok():
    import inspect
    src = inspect.getsource(exp_mod.ExperimentLogger.__init__)
    assert "exist_ok=True" not in src


def test_recording_ownership_is_recorded(tmp_path, src_file):
    log = ExperimentLogger("r", "", src_file, base_dir=str(tmp_path), mode="real")
    log.record_recording({"started_by_this_run": False, "save_dir": "X", "start_status": 409})
    meta, _, summary = _finish(log)
    assert meta["sensor_recording"]["started_by_this_run"] is False
    assert "このランは開始していない" in summary


# ----------------------------------------------------------------------
# run_flow: /api/end only for a recording this run started
# ----------------------------------------------------------------------
def test_409_on_start_means_not_ours_and_end_is_not_sent(monkeypatch):
    posts = []

    def post(url, body, timeout=5.0):
        posts.append(url)
        return (409, {}) if url.endswith("/api/start") else (200, {})
    monkeypatch.setattr(run_flow, "_http_post_json", post)
    monkeypatch.setattr(run_flow, "_http_get_json", lambda url, timeout=2.0: {"save_dir": "D"})

    handle = run_flow.start_csv_recording("exp")
    assert handle.owned is False and handle.save_dir == "D"
    assert run_flow.stop_csv_recording(handle) is False
    assert not any(u.endswith("/api/end") for u in posts)


def test_200_on_start_is_ours_and_end_is_sent(monkeypatch):
    posts = []

    def post(url, body, timeout=5.0):
        posts.append(url)
        return 200, {"save_dir": "S", "saved_path": "P"}
    monkeypatch.setattr(run_flow, "_http_post_json", post)

    handle = run_flow.start_csv_recording("exp")
    assert handle.owned is True and handle.as_metadata()["started_by_this_run"] is True
    assert run_flow.stop_csv_recording(handle) is True
    assert posts[-1].endswith("/api/end")


def test_stop_without_a_handle_sends_nothing(monkeypatch):
    monkeypatch.setattr(run_flow, "_http_post_json",
                        lambda *a, **k: pytest.fail("/api/end must not be sent"))
    assert run_flow.stop_csv_recording(None) is False


def test_failed_start_is_not_ours(monkeypatch):
    monkeypatch.setattr(run_flow, "_http_post_json", lambda *a, **k: (500, {}))
    assert run_flow.start_csv_recording("exp").owned is False


# ----------------------------------------------------------------------
# A capture that returns None is a failed step
# ----------------------------------------------------------------------
class _NoImageShared:
    async def capture_and_save(self, file_path=None):
        return None

    async def capture_microscope(self, file_path=None):
        return None


@pytest.mark.parametrize("action", ["capture_and_save", "capture_microscope"])
def test_capture_returning_none_raises(action):
    with pytest.raises(RuntimeError):
        asyncio.run(execute_step({"action": action, "file_path": ""}, {}, _NoImageShared()))


def test_logger_never_records_an_imageless_capture_as_ok(tmp_path, src_file):
    log = ExperimentLogger("c", "", src_file, base_dir=str(tmp_path), mode="mock")
    log.record_step(1, 1, "capture_and_save", None, None, "ok", 0.1, result={"image_path": None})
    meta, rows, _ = _finish(log)
    assert rows[0]["status"] == "error" and rows[0]["error"]
    assert meta["error_steps"] == 1


def test_mock_cli_run_records_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(run_flow, "LOGS_DIR", str(tmp_path / "logs"))
    flow = tmp_path / "f.json"
    flow.write_text(json.dumps({"name": "modecheck", "steps": [
        {"action": "go_home", "robot_id": 1}]}), encoding="utf-8")
    assert run_flow.main([str(flow), "--mock"]) == 0
    metas = [os.path.join(r, "metadata.json") for r, _, fs in os.walk(tmp_path / "logs")
             if "metadata.json" in fs]
    assert len(metas) == 1
    with open(metas[0], encoding="utf-8") as f:
        assert json.load(f)["mode"] == "mock"
