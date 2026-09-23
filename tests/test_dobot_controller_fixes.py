"""PyDobotController / home_dobot の修正点（ハードウェア不要）。

* set_speed_preset が Joint PTP パラメータも設定する
* 応答 None を成功扱いにしない（グリッパー・吸引・スライダー・コンベア）
* 初期化失敗時にシリアルを閉じる
* home() の完了待ちが force_stop の停止フラグで打ち切られる
* home_dobot CLI はホーミング中のあらゆる例外で force_stop する
"""
import pytest

pytest.importorskip("pydobot")

from src.devices.dobot import home_dobot
from src.devices.dobot import pydobot_controller as pc
from src.devices.dobot import pydobot_patch as pp
from src.devices.dobot.dobot_config import DobotConfig
from src.devices.dobot.pydobot_controller import PyDobotController
from src.devices.safety.validators import WorkspaceValidator

from tests.test_home_dobot import FakeDobot, _Response


class SpeedDevice:
    def __init__(self, reply=True, fail_speed=False):
        self.reply = reply
        self.fail_speed = fail_speed
        self.calls = []
        self.closed = False

    def speed(self, velocity, acceleration):
        if self.fail_speed:
            raise pp.DobotReplyError("no reply")
        self.calls.append(("speed", velocity, acceleration))

    def _set_ptp_joint_params(self, *a):
        self.calls.append(("joint", *a))

    def _set_end_effector_gripper(self, enable):
        self.calls.append(("grip", enable))
        return _Response(b"") if self.reply else None

    def _set_end_effector_suction_cup(self, enable):
        self.calls.append(("suck", enable))
        return _Response(b"") if self.reply else None

    def _send_command(self, msg, wait=False):
        self.calls.append(("send", msg.id, wait))
        return _Response(b"") if self.reply else None

    def pose(self):
        return (200.0, 0.0, 50.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def close(self):
        self.closed = True


def _ctrl(dev):
    ctrl = object.__new__(PyDobotController)
    ctrl.port_name = "TEST"
    ctrl.device = dev
    ctrl._connected = True
    ctrl.verbose = False
    return ctrl


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)


def test_set_speed_preset_applies_joint_params():
    dev = SpeedDevice()
    ctrl = _ctrl(dev)
    name = next(iter(DobotConfig.SPEED_PRESETS))
    ctrl.set_speed_preset(name)
    cfg = DobotConfig.get_speed_preset(name)
    joint = [c for c in dev.calls if c[0] == "joint"]
    assert joint == [("joint", *cfg["joint_velocity"][:4], *cfg["joint_acceleration"][:4])]
    assert ("speed", cfg["linear_velocity"], cfg["linear_acceleration"]) in dev.calls


@pytest.mark.parametrize("method", ["set_gripper", "set_suction_cup"])
def test_end_effector_none_reply_is_not_success(method):
    with pytest.raises(ConnectionError):
        getattr(_ctrl(SpeedDevice(reply=False)), method)(True, True)


@pytest.mark.parametrize("method,tag", [("set_gripper", "grip"), ("set_suction_cup", "suck")])
def test_end_effector_confirmed_reply(method, tag):
    dev = SpeedDevice()
    assert getattr(_ctrl(dev), method)(True, False) == 0
    assert (tag, False) in dev.calls


def test_move_slider_none_reply_raises():
    with pytest.raises(ConnectionError):
        _ctrl(SpeedDevice(reply=False)).move_slider(100)


def test_move_conveyer_none_reply_raises():
    with pytest.raises(ConnectionError):
        _ctrl(SpeedDevice(reply=False)).move_conveyer(0, 10, 0)


def test_failed_speed_setup_closes_serial(monkeypatch):
    created = []

    def factory(port, verbose=False):
        d = SpeedDevice(fail_speed=True)
        created.append(d)
        return d

    monkeypatch.setattr(pc, "Dobot", factory)
    with pytest.raises(ConnectionError):
        PyDobotController(port_name="TEST")
    assert created[0].closed, "COM ポートを閉じてから device を捨てる"


def test_move_timeout_is_passed_to_device(monkeypatch):
    dev = SpeedDevice()
    monkeypatch.setattr(pc, "Dobot", lambda port, verbose=False: dev)
    PyDobotController(port_name="TEST", move_timeout_s=12.5)
    assert dev.move_timeout_s == 12.5


def test_move_to_work_position_removed():
    """J1 に r を渡し検証も通らない未使用メソッドは削除した。"""
    assert not hasattr(PyDobotController, "move_to_work_position")


def test_home_wait_aborts_on_stop_request():
    fake = FakeDobot(never_finish=True)
    ctrl = _ctrl(fake)
    orig = fake._get_queued_cmd_current_index

    def poll():
        if fake.polls == 1:
            pp.request_stop(fake)
        return orig()

    fake._get_queued_cmd_current_index = poll
    with pytest.raises(pp.DobotMoveAborted):
        ctrl.home(timeout_s=1e9, poll_interval=0)


def test_home_clears_old_stop_request():
    fake = FakeDobot()
    pp.request_stop(fake)
    _ctrl(fake).home(timeout_s=5, poll_interval=0)
    assert not pp.stop_requested(fake)


# ---------------------------------------------------------------------------
# home_dobot CLI
# ---------------------------------------------------------------------------
class FailingHome(home_dobot.MockHomingController):
    def __init__(self, exc, stop_ok=True):
        super().__init__("COMX")
        self.exc = exc
        self.stop_ok = stop_ok
        self.force_stopped = 0
        self.disconnected = False

    def home(self, *a, **k):
        raise self.exc

    def force_stop(self):
        self.force_stopped += 1
        if isinstance(self.stop_ok, Exception):
            raise self.stop_ok
        return self.stop_ok

    def disconnect(self):
        assert self.force_stopped, "切断より先に force_stop する"
        self.disconnected = True


def _run(monkeypatch, ctrl):
    monkeypatch.setattr(home_dobot, "default_workspace_validator", lambda: WorkspaceValidator())
    monkeypatch.setattr(home_dobot, "_connect", lambda port, mock: ctrl)
    return home_dobot.main(["--robot", "1", "--port", "COMX", "--yes"])


@pytest.mark.parametrize("exc,rc", [
    (TimeoutError("slow"), 2),
    (ConnectionError("no reply"), 1),
    (pp.DobotReplyError("no reply"), 1),
    (RuntimeError("other"), 1),
])
def test_cli_force_stops_on_any_homing_error(monkeypatch, exc, rc):
    ctrl = FailingHome(exc)
    assert _run(monkeypatch, ctrl) == rc
    assert ctrl.force_stopped == 1
    assert ctrl.disconnected


@pytest.mark.parametrize("stop_result", [False, IOError("port gone")])
def test_cli_warns_when_stop_unconfirmed(monkeypatch, capsys, stop_result):
    ctrl = FailingHome(ConnectionError("no reply"), stop_ok=stop_result)
    assert _run(monkeypatch, ctrl) == 1
    assert "停止を確認できませんでした" in capsys.readouterr().out
