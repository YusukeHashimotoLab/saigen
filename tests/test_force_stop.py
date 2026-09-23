"""PyDobotController.force_stop: 緊急停止プロトコルの送信内容"""
import pytest

pydobot = pytest.importorskip("pydobot")

from src.devices.dobot.pydobot_controller import PyDobotController


class FakeDevice:
    """pydobot.Dobot の送信系だけを模したフェイク"""

    def __init__(self, fail_send=False, fail_clear=False):
        self.fail_send = fail_send
        self.fail_clear = fail_clear
        self.sent = []
        self.clear_count = 0

    def _send_command(self, msg):
        if self.fail_send:
            raise IOError("serial gone")
        self.sent.append(msg)
        return object()   # 応答あり

    def _set_queued_cmd_clear(self):
        if self.fail_clear:
            raise IOError("serial gone")
        self.clear_count += 1
        return object()


def make_controller(device):
    """実機接続（__init__ 内の _initialize）を経由せずにインスタンス化する"""
    ctrl = object.__new__(PyDobotController)
    ctrl.port_name = "TEST"
    ctrl.device = device
    ctrl._connected = device is not None
    return ctrl


def test_force_stop_sends_force_stop_exec_immediately():
    dev = FakeDevice()
    ctrl = make_controller(dev)
    assert ctrl.force_stop() is True

    # 1通目: QueuedCmdForceStopExec (ID:242) を即時 (ctrl=0x01) で送信
    first = dev.sent[0]
    assert first.id == 242
    assert first.ctrl == 0x01


def test_force_stop_clears_queue():
    dev = FakeDevice()
    ctrl = make_controller(dev)
    ctrl.force_stop()
    assert dev.clear_count == 1


def test_force_stop_stops_both_conveyers():
    dev = FakeDevice()
    ctrl = make_controller(dev)
    ctrl.force_stop()

    emotor = [m for m in dev.sent if m.id == 135]
    assert len(emotor) == 2
    for i, msg in enumerate(emotor):
        assert msg.ctrl == 0x01          # 即時実行
        assert msg.params[0] == i        # index 0, 1
        assert msg.params[1] == 0x00     # isEnabled=0（停止）


def test_force_stop_without_device_returns_false():
    ctrl = make_controller(None)
    assert ctrl.force_stop() is False


def test_force_stop_never_raises_on_send_failure():
    """緊急停止経路は通信エラーでも例外を出さない（ベストエフォート）"""
    dev = FakeDevice(fail_send=True)
    ctrl = make_controller(dev)
    assert ctrl.force_stop() is False
    # 送信が失敗してもキュー破棄は試みられる
    assert dev.clear_count == 1


def test_force_stop_partial_failure_returns_false():
    dev = FakeDevice(fail_clear=True)
    ctrl = make_controller(dev)
    assert ctrl.force_stop() is False
    # キュー破棄が失敗しても ForceStopExec とコンベア停止は送信済み
    assert any(m.id == 242 for m in dev.sent)
    assert len([m for m in dev.sent if m.id == 135]) == 2


class SilentDevice(FakeDevice):
    """送信は例外にならないが応答が無い（None）。"""

    def _send_command(self, msg):
        self.sent.append(msg)
        return None

    def _set_queued_cmd_clear(self):
        self.clear_count += 1
        return None


def test_force_stop_without_reply_is_not_success():
    """応答 None を停止確認とみなさない"""
    ctrl = make_controller(SilentDevice())
    assert ctrl.force_stop() is False


def test_force_stop_sets_stop_flag_before_sending():
    from src.devices.dobot import pydobot_patch

    seen = []

    class Spy(FakeDevice):
        def _send_command(self, msg):
            seen.append(pydobot_patch.stop_requested(self))
            return super()._send_command(msg)

    dev = Spy()
    ctrl = make_controller(dev)
    ctrl.force_stop()
    assert seen and all(seen), "停止フラグは停止コマンドより先に立つ"
    assert ctrl.stop_requested
