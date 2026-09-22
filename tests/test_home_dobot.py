"""Dobot homing: PyDobotController.home() と home_dobot CLI（ハードウェア不要）。

背景: pydobot 1.3.x には home() が無く、PyDobotController(homing=True) は
AttributeError で接続失敗になっていた。home() を Dobot プロトコル
(SetHOMEParams ID 30 / SetHOMECmd ID 31) で実装し、CLI からは戻り先を
WorkspaceValidator で事前検証する。
"""
import struct

import pytest

from src.devices.dobot import home_dobot
from src.devices.dobot import pydobot_controller as pc
from src.devices.dobot.dobot_config import DobotConfig
from src.devices.dobot.pydobot_controller import PyDobotController
from src.devices.safety.validators import WorkspaceValidator, WorkspaceViolationError

START_POSE = (193.1, -0.0, 115.1, -0.0, -0.0, 20.2, 4.5, -0.0)   # 2026-09-22 の実機 COM7 の応答


class _Response:
    def __init__(self, params: bytes):
        self.params = params


class FakeDobot:
    """pydobot.Dobot のうち home() が触る部分だけを模擬する。

    キュー index は SetHOMECmd ごとに 1 進み、_get_queued_cmd_current_index は
    ``steps_until_done`` 回呼ばれるまで古い index を返す（完了待ちのテスト用）。
    """

    def __init__(self, steps_until_done: int = 2, pose=START_POSE, never_finish: bool = False):
        self.sent = []                 # (id, ctrl, params bytes)
        self.queue_idx = 6
        self.current_idx = 6
        self.polls = 0
        self.steps_until_done = steps_until_done
        self.never_finish = never_finish
        self._pose = tuple(pose)
        self.closed = False

    # --- pydobot.Dobot API ---------------------------------------------
    def _send_command(self, msg, wait=False):
        msg.refresh()
        self.sent.append((msg.id, msg.ctrl, bytes(msg.params)))
        if msg.id == 31:
            self.queue_idx += 1
            return _Response(struct.pack("<Q", self.queue_idx))
        return _Response(b"")

    def _get_queued_cmd_current_index(self):
        self.polls += 1
        if not self.never_finish and self.polls >= self.steps_until_done:
            self.current_idx = self.queue_idx
        return self.current_idx

    def pose(self):
        return self._pose

    def speed(self, velocity, acceleration):
        pass

    def _set_ptp_joint_params(self, *a):
        pass

    def close(self):
        self.closed = True


def _controller(fake: FakeDobot) -> PyDobotController:
    ctrl = object.__new__(PyDobotController)
    ctrl.port_name = "TEST"
    ctrl.device = fake
    ctrl._connected = True
    ctrl.verbose = False
    return ctrl


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)


# ---------------------------------------------------------------------------
# PyDobotController.home()
# ---------------------------------------------------------------------------
def test_home_sends_params_then_cmd_and_waits():
    fake = FakeDobot(steps_until_done=3)
    ctrl = _controller(fake)

    final = ctrl.home(250.0, 0.0, 50.0, 0.0, timeout_s=5, poll_interval=0)

    ids = [s[0] for s in fake.sent]
    assert ids == [30, 31], "SetHOMEParams (30) の後に SetHOMECmd (31) を送る"
    assert all(s[1] == 0x03 for s in fake.sent), "両方とも rw=1, isQueued=1"
    assert struct.unpack("<4f", fake.sent[0][2]) == pytest.approx((250.0, 0.0, 50.0, 0.0))
    assert fake.sent[1][2] == struct.pack("<I", 0)
    assert fake.polls >= 3, "キュー index が SetHOMECmd の index に達するまでポーリングする"
    assert final == list(START_POSE)


def test_home_defaults_to_current_pose_as_return_target():
    fake = FakeDobot()
    ctrl = _controller(fake)

    ctrl.home(timeout_s=5, poll_interval=0)

    params = struct.unpack("<4f", fake.sent[0][2])
    assert params == pytest.approx(START_POSE[:4], abs=1e-4)


def test_home_partial_target_keeps_other_axes():
    fake = FakeDobot()
    ctrl = _controller(fake)

    ctrl.home(z=80.0, timeout_s=5, poll_interval=0)

    x, y, z, r = struct.unpack("<4f", fake.sent[0][2])
    assert (x, y, r) == pytest.approx((START_POSE[0], START_POSE[1], START_POSE[3]), abs=1e-4)
    assert z == pytest.approx(80.0)


def test_home_times_out_if_queue_never_advances(monkeypatch):
    fake = FakeDobot(never_finish=True)
    ctrl = _controller(fake)
    clock = iter([0.0, 0.0, 0.5, 1.5, 2.5, 3.5])  # monotonic の返り値
    monkeypatch.setattr(pc.time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError):
        ctrl.home(timeout_s=1.0, poll_interval=0)


def test_home_raises_when_disconnected():
    ctrl = object.__new__(PyDobotController)
    ctrl.port_name = "TEST"
    ctrl.device = None
    ctrl._connected = False
    ctrl.verbose = False
    with pytest.raises(ConnectionError):
        ctrl.home()


def test_home_raises_when_home_cmd_gets_no_response():
    class Silent(FakeDobot):
        def _send_command(self, msg, wait=False):
            msg.refresh()
            self.sent.append((msg.id, msg.ctrl, bytes(msg.params)))
            return None if msg.id == 31 else _Response(b"")

    ctrl = _controller(Silent())
    with pytest.raises(ConnectionError):
        ctrl.home(timeout_s=1, poll_interval=0)


def test_constructor_homing_true_uses_own_home(monkeypatch):
    """回帰: homing=True が pydobot に無い Dobot.home() を呼んで落ちていた。"""
    created = []

    def fake_dobot_factory(port, verbose=False):
        d = FakeDobot()
        created.append(d)
        return d

    monkeypatch.setattr(pc, "Dobot", fake_dobot_factory)

    ctrl = PyDobotController(port_name="TEST", homing=True)

    assert ctrl.api is not None
    assert [s[0] for s in created[0].sent] == [30, 31]


# ---------------------------------------------------------------------------
# home_dobot CLI: 純粋関数
# ---------------------------------------------------------------------------
def test_compute_return_target_current_keeps_pose():
    t = home_dobot.compute_return_target(START_POSE, "current")
    assert (t["x"], t["y"], t["z"], t["r"], t["joint1"]) == START_POSE[:5]


def test_compute_return_target_zero_keeps_radius_and_height():
    pose = (150.0, 150.0, 80.0, 12.0, 45.0, 0, 0, 0)
    t = home_dobot.compute_return_target(pose, "zero")
    assert t["x"] == pytest.approx(150.0 * 2 ** 0.5)
    assert t["y"] == 0.0
    assert t["z"] == 80.0
    assert t["r"] == 0.0
    assert t["joint1"] == 0.0


def test_compute_return_target_config_uses_home_settings():
    t = home_dobot.compute_return_target(START_POSE, "config")
    h = DobotConfig.HOME_SETTINGS
    assert (t["x"], t["y"], t["z"], t["r"]) == (h["home_x"], h["home_y"], h["home_z"], h["home_r"])
    assert t["joint1"] == pytest.approx(0.0)


def test_compute_return_target_rejects_unknown():
    with pytest.raises(ValueError):
        home_dobot.compute_return_target(START_POSE, "elsewhere")


def test_validate_return_target_rejects_out_of_workspace():
    v = WorkspaceValidator(z_min=-130, z_max=100)
    with pytest.raises(WorkspaceViolationError):
        home_dobot.validate_return_target({"x": 200, "y": 0, "z": 115.1, "r": 0, "joint1": 0}, v)


def test_validate_return_target_rejects_joint1_outside_limits():
    v = WorkspaceValidator(joint1_min=-48, joint1_max=110)
    with pytest.raises(WorkspaceViolationError):
        home_dobot.validate_return_target({"x": 200, "y": 0, "z": 50, "r": 0, "joint1": -60}, v)


def test_validate_return_target_accepts_inside():
    v = WorkspaceValidator()
    home_dobot.validate_return_target({"x": 200, "y": 0, "z": 50, "r": 0, "joint1": 0}, v)


def test_resolve_dobot_port_priority(monkeypatch):
    monkeypatch.setattr(home_dobot.lab_config, "get_robot_ports",
                        lambda: {1: {"dobot_port": "COM_CFG"}})
    monkeypatch.delenv("ROBOT1_DOBOT_PORT", raising=False)
    assert home_dobot.resolve_dobot_port(1) == "COM_CFG"

    monkeypatch.setenv("ROBOT1_DOBOT_PORT", "COM_ENV")
    assert home_dobot.resolve_dobot_port(1) == "COM_ENV"

    assert home_dobot.resolve_dobot_port(1, "COM_CLI") == "COM_CLI"


def test_resolve_dobot_port_missing_robot(monkeypatch):
    monkeypatch.setattr(home_dobot.lab_config, "get_robot_ports", lambda: {1: {"dobot_port": "COM1"}})
    monkeypatch.delenv("ROBOT2_DOBOT_PORT", raising=False)
    with pytest.raises(KeyError):
        home_dobot.resolve_dobot_port(2)


# ---------------------------------------------------------------------------
# home_dobot CLI: --mock で端から端まで
# ---------------------------------------------------------------------------
def test_cli_mock_run_completes(monkeypatch, capsys):
    monkeypatch.setattr(home_dobot, "default_workspace_validator", lambda: WorkspaceValidator())
    rc = home_dobot.main(["--robot", "1", "--port", "COMX", "--mock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[MOCK]" in out
    assert "ホーミング完了" in out


def test_cli_mock_refuses_out_of_workspace_target(monkeypatch, capsys):
    tight = WorkspaceValidator(z_min=-130, z_max=100)   # 開始姿勢 Z=115.1 は範囲外
    monkeypatch.setattr(home_dobot, "default_workspace_validator", lambda: tight)
    rc = home_dobot.main(["--robot", "1", "--port", "COMX", "--mock"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "何も送らずに終了" in out


def test_cli_mock_zero_target_sends_zero_y(monkeypatch):
    monkeypatch.setattr(home_dobot, "default_workspace_validator", lambda: WorkspaceValidator())
    captured = {}

    real_connect = home_dobot._connect

    def spy_connect(port, mock):
        ctrl = real_connect(port, mock)
        captured["ctrl"] = ctrl
        return ctrl

    monkeypatch.setattr(home_dobot, "_connect", spy_connect)
    rc = home_dobot.main(["--robot", "2", "--port", "COMX", "--mock", "--target", "zero"])
    assert rc == 0
    assert captured["ctrl"].homed_with["y"] == 0.0
    assert captured["ctrl"].homed_with["r"] == 0.0


def test_cli_real_mode_waits_for_enter_before_sending(monkeypatch, capsys):
    """実機モードでは Enter を押すまで home() を呼ばない（Ctrl+C で中止できる）。"""
    monkeypatch.setattr(home_dobot, "default_workspace_validator", lambda: WorkspaceValidator())
    ctrl = home_dobot.MockHomingController("COMX")
    monkeypatch.setattr(home_dobot, "_connect", lambda port, mock: ctrl)
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(KeyboardInterrupt()))

    rc = home_dobot.main(["--robot", "1", "--port", "COMX"])

    assert rc == 130
    assert ctrl.homed_with is None
    assert "コマンドは送っていません" in capsys.readouterr().out


def test_cli_ctrl_c_during_homing_force_stops(monkeypatch):
    monkeypatch.setattr(home_dobot, "default_workspace_validator", lambda: WorkspaceValidator())

    class Interrupting(home_dobot.MockHomingController):
        def __init__(self, port):
            super().__init__(port)
            self.force_stopped = False

        def home(self, *a, **k):
            raise KeyboardInterrupt()

        def force_stop(self):
            self.force_stopped = True
            return True

    ctrl = Interrupting("COMX")
    monkeypatch.setattr(home_dobot, "_connect", lambda port, mock: ctrl)

    rc = home_dobot.main(["--robot", "1", "--port", "COMX", "--yes"])

    assert rc == 130
    assert ctrl.force_stopped


def test_cli_connection_failure_returns_1(monkeypatch, capsys):
    def boom(port, mock):
        raise ConnectionError("no device")

    monkeypatch.setattr(home_dobot, "_connect", boom)
    rc = home_dobot.main(["--robot", "1", "--port", "COMX", "--yes"])
    assert rc == 1
    assert "接続失敗" in capsys.readouterr().out
