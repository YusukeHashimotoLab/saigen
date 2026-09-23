"""LabRobot: blocking driver calls run in a worker thread and are interruptible.

A cancellation (Ctrl+C) while a pydobot ``wait=True`` move is in flight must
take effect immediately: the awaiting side sends ``force_stop()``, waits for
the worker, and re-raises. Afterwards the pose is unknown (re-read before the
next move), and an interrupted / failed pipette operation leaves the held
volume unknown until blow_out or reset. cleanup() stops the IKA before closing
it. All fakes; no hardware.
"""
import asyncio
import threading
import time

import pytest

from src.devices.safety.lab_robot import LabRobot
from src.devices.safety.validators import WorkspaceValidator


class BlockingDobot:
    """PyDobotController stand-in whose moves block like pydobot's wait=True."""

    speed_preset = "中速"

    def __init__(self, pose=(200.0, 0.0, 50.0, 0.0, 0.0), release_on_stop=True):
        x, y, z, r, j1 = pose
        self.pose = [x, y, z, r, j1, 0.0, 0.0, 0.0]
        self.release_on_stop = release_on_stop
        self.started = threading.Event()
        self.released = threading.Event()
        self.finished = threading.Event()
        self.force_stops = 0
        self.commands = []
        self.pose_reads = 0
        self.block = True

    def get_current_position(self):
        self.pose_reads += 1
        return list(self.pose)

    def _blocking(self, *cmd):
        self.commands.append(cmd)
        self.started.set()
        try:
            if self.block:
                self.released.wait(10)
        finally:
            self.finished.set()

    def move_Z(self, dz):
        self._blocking("Z", dz)
        self.pose[2] += dz

    def move_angle(self, a):
        self._blocking("J1", a)

    def move_XYZ_abs(self, x, y, z):
        self._blocking("XYZ", x, y, z)

    def move_slider(self, pos):
        self._blocking("slider", pos)

    def move_conveyer(self, index, speed, duration):
        self._blocking("conveyer", index, speed, duration)

    def set_speed_preset(self, p):
        pass

    def force_stop(self):
        self.force_stops += 1
        if self.release_on_stop:
            self.released.set()
        return True


def _robot(dobot, **cfg):
    robot = LabRobot(use_dobot=True, workspace_validator=WorkspaceValidator(),
                     pose_settle_interval=0, **cfg)
    robot.dobot = dobot
    for attr in ("wait_after_angle_move", "wait_after_z_move", "wait_after_xy_move",
                 "wait_after_slider_move", "wait_after_conveyer", "wait_after_pipette"):
        setattr(robot, attr, 0)
    robot.home_position = [200.0, 0.0, 100.0, 0.0]
    return robot


async def _cancel_when_started(dobot, coro, ticks):
    task = asyncio.ensure_future(coro)
    t0 = time.monotonic()
    while not dobot.started.is_set():
        assert time.monotonic() - t0 < 5, "the move never started"
        await asyncio.sleep(0.01)
    # The event loop is not blocked by the move: this coroutine keeps running.
    for _ in range(3):
        await asyncio.sleep(0.01)
        ticks.append(True)
    task.cancel()
    t1 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    return time.monotonic() - t1


@pytest.mark.parametrize("method, args", [
    ("move_z", (-10.0,)),
    ("move_xyz", (200.0, 50.0, 20.0)),
    ("rotate", (30.0,)),
    ("rotate_relative", (30.0,)),
    ("move_radial", (10.0,)),
    ("move_slider", (100.0,)),
    ("go_home", ()),
])
def test_cancel_during_a_blocking_move_force_stops_immediately(method, args):
    dobot = BlockingDobot()
    robot = _robot(dobot)
    ticks = []
    elapsed = asyncio.run(_cancel_when_started(dobot, getattr(robot, method)(*args), ticks))
    assert ticks == [True, True, True], "the event loop was blocked by the move"
    assert dobot.force_stops == 1
    assert dobot.finished.is_set(), "the worker must have returned before re-raising"
    assert elapsed < 5
    assert len(dobot.commands) == 1, "no further leg / recovery move after the cancel"
    if method != "move_slider":
        assert robot._pose_unknown


def test_cancel_during_conveyer_force_stops_and_keeps_pose_known():
    dobot = BlockingDobot()
    robot = _robot(dobot)
    asyncio.run(_cancel_when_started(dobot, robot.move_conveyer(0, 50, 300), []))
    assert dobot.force_stops == 1
    assert not robot._pose_unknown   # the arm itself did not move


def test_worker_that_never_returns_is_abandoned_after_the_timeout():
    dobot = BlockingDobot(release_on_stop=False)
    robot = _robot(dobot, stop_wait_timeout=0.2)
    try:
        elapsed = asyncio.run(_cancel_when_started(dobot, robot.move_z(-10.0), []))
        assert dobot.force_stops == 1
        assert elapsed < 3
    finally:
        dobot.released.set()


def test_pose_is_reread_before_the_next_move_after_a_cancel():
    dobot = BlockingDobot()
    robot = _robot(dobot)
    asyncio.run(_cancel_when_started(dobot, robot.move_z(-10.0), []))
    assert robot._pose_unknown
    dobot.block = False
    reads_before = dobot.pose_reads
    asyncio.run(robot.move_z(-5.0))
    # stable-pose check: at least two reads before the validated move
    assert dobot.pose_reads - reads_before >= 2
    assert not robot._pose_unknown


def test_unstable_pose_after_a_cancel_refuses_the_next_move():
    dobot = BlockingDobot()
    robot = _robot(dobot, pose_settle_reads=3)
    asyncio.run(_cancel_when_started(dobot, robot.move_z(-10.0), []))
    dobot.block = False
    orig = dobot.get_current_position

    def drifting():
        dobot.pose[0] += 5.0          # still moving
        return orig()

    dobot.get_current_position = drifting
    n = len(dobot.commands)
    with pytest.raises(RuntimeError, match="安定しません"):
        asyncio.run(robot.move_z(-5.0))
    assert len(dobot.commands) == n, "no move may be sent while the pose is unstable"


def test_failed_move_marks_pose_unknown():
    class Failing(BlockingDobot):
        def move_Z(self, dz):
            raise IOError("serial error")

    dobot = Failing()
    dobot.block = False
    robot = _robot(dobot)
    robot.go_home = _noop_home
    with pytest.raises(IOError):
        asyncio.run(robot.move_z(-10.0))
    assert robot._pose_unknown


async def _noop_home():
    pass


def test_estop_from_another_thread_mid_move_stops_the_flow_without_auto_home():
    """GUI Stop: emergency_stop() from the UI thread; the driver returns normally."""
    dobot = BlockingDobot()
    robot = _robot(dobot)

    async def scenario():
        task = asyncio.ensure_future(robot.move_z(-10.0))
        while not dobot.started.is_set():
            await asyncio.sleep(0.01)
        threading.Thread(target=robot.emergency_stop).start()
        with pytest.raises(RuntimeError, match="緊急停止"):
            await task

    asyncio.run(scenario())
    assert dobot.force_stops == 1
    # no automatic go_home after an emergency stop
    assert dobot.commands == [("Z", -10.0)]
    assert robot._pose_unknown


def test_successful_move_returns_normally():
    dobot = BlockingDobot()
    dobot.block = False
    robot = _robot(dobot)
    asyncio.run(robot.move_z(-10.0))
    assert dobot.commands == [("Z", -10.0)]
    assert dobot.force_stops == 0
    assert not robot._pose_unknown


# ----------------------------------------------------------------------
# Held volume after an interrupted / failed pipette operation
# ----------------------------------------------------------------------
class FakePicus:
    def __init__(self, fail=None, block=None):
        self.fail = fail            # method name that raises
        self.block = block          # method name that blocks (awaits forever)
        self.calls = []

    async def set_motor_mode(self, mode):
        pass

    async def _op(self, name, *args):
        self.calls.append(name)
        if name == self.fail:
            raise IOError("picus gone")
        if name == self.block:
            await asyncio.sleep(3600)

    async def aspirate(self, v, speed=5):
        await self._op("aspirate", v)

    async def dispense(self, v, speed=5):
        await self._op("dispense", v)

    async def blow_out(self, go_home=True, speed=1, delay_ms=3000):
        await self._op("blow_out")


def _pipette_robot(picus):
    robot = LabRobot(use_picus2=True)
    robot.picus2 = picus
    robot.wait_after_pipette = 0
    return robot


def test_failed_aspirate_makes_the_volume_unknown_and_refuses_liquid_ops():
    robot = _pipette_robot(FakePicus(fail="aspirate"))
    with pytest.raises(IOError):
        asyncio.run(robot.aspirate(1.0))
    assert robot.pipette_volume == 0.0           # last confirmed value
    assert robot.pipette_volume_known is False
    robot.picus2.fail = None
    with pytest.raises(RuntimeError, match="保持量が不明"):
        asyncio.run(robot.aspirate(1.0))
    with pytest.raises(RuntimeError, match="保持量が不明"):
        asyncio.run(robot.dispense(1.0))
    assert robot.picus2.calls == ["aspirate"], "no command after the volume became unknown"


def test_cancelled_dispense_makes_the_volume_unknown():
    robot = _pipette_robot(FakePicus(block="dispense"))
    robot._current_pipette_volume = 5.0

    async def scenario():
        task = asyncio.ensure_future(robot.dispense(2.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert robot.pipette_volume_known is False
    assert robot.pipette_volume == 5.0


def test_blow_out_and_reset_clear_the_unknown_volume():
    robot = _pipette_robot(FakePicus(fail="aspirate"))
    with pytest.raises(IOError):
        asyncio.run(robot.aspirate(1.0))
    robot.picus2.fail = None
    asyncio.run(robot.blow_out())
    assert robot.pipette_volume_known and robot.pipette_volume == 0.0
    asyncio.run(robot.aspirate(1.0))
    assert robot.pipette_volume == 1.0

    robot._mark_pipette_volume_unknown("test")
    robot.reset_pipette_volume(0.0)
    assert robot.pipette_volume_known


def test_emergency_stop_during_a_pipette_operation_marks_the_volume_unknown():
    robot = _pipette_robot(FakePicus(block="aspirate"))

    async def scenario():
        task = asyncio.ensure_future(robot.aspirate(1.0))
        await asyncio.sleep(0.05)
        robot.emergency_stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert robot.pipette_volume_known is False


def test_parameter_errors_do_not_make_the_volume_unknown():
    robot = _pipette_robot(FakePicus())
    with pytest.raises(ValueError):
        asyncio.run(robot.aspirate(0.1))
    with pytest.raises(ValueError):
        asyncio.run(robot.dispense(1.0))       # nothing held
    assert robot.pipette_volume_known


# ----------------------------------------------------------------------
# IKA is stopped by cleanup(), before its port is closed
# ----------------------------------------------------------------------
class FakeIKA:
    def __init__(self, calls, fail_stop=False):
        self.calls = calls
        self.fail_stop = fail_stop

    def stop_stirring(self):
        self.calls.append("stop_stirring")
        if self.fail_stop:
            raise IOError("ika gone")

    def disconnect(self):
        self.calls.append("disconnect")


def test_cleanup_stops_ika_before_closing_it():
    calls = []
    robot = LabRobot()
    robot.ika = FakeIKA(calls)
    asyncio.run(robot.cleanup())
    assert calls == ["stop_stirring", "disconnect"]


def test_cleanup_still_closes_ika_when_the_stop_fails():
    calls = []
    robot = LabRobot()
    robot.ika = FakeIKA(calls, fail_stop=True)
    asyncio.run(robot.cleanup())
    assert calls == ["stop_stirring", "disconnect"]


def test_emergency_stop_stops_ika():
    calls = []
    robot = LabRobot()
    robot.ika = FakeIKA(calls)
    robot.emergency_stop()
    assert calls == ["stop_stirring"]
