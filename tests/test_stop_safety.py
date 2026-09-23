"""Stop / recovery safety fixes (hardware-free).

1. GUI Stop emergency-stops the robots from the UI thread *before* cancelling
   the asyncio task, even while the event loop is blocked; Stop is idempotent;
   the stop flag is checked between ``pre_step`` and the step dispatch.
2. ExperimentSession.run: an interruption during the error-recovery homing
   emergency-stops the robots before cleanup; cleanup survives cancellation.
3. A WorkspaceViolationError never triggers an automatic go_home, and go_home
   validates every leg (and the captured home) against the workspace limits.
"""
import asyncio
import logging
import threading
import time

import pytest

from src.devices.safety.lab_robot import LabRobot
from src.devices.safety.mock_robot import MockLabRobot
from src.devices.safety.validators import WorkspaceValidator, WorkspaceViolationError
from src.flow.experiment_session import ExperimentSession
from src.gui import runner as gui_runner


# ======================================================================
# 1. GUI runner Stop
# ======================================================================
class _BlockingRobot(MockLabRobot):
    """move_z blocks the event loop synchronously, like a pydobot move."""

    def __init__(self, calls, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = calls
        self.blocking = threading.Event()
        self.released = threading.Event()
        self.move_z_calls = 0
        self.estop_while_blocked = False
        self.emergency_stops = 0
        self.cleanups = 0

    async def move_z(self, distance):
        self.move_z_calls += 1
        self.blocking.set()
        # Synchronous wait: the asyncio task cannot be cancelled while here.
        self.released.wait(10)
        self.blocking.clear()

    def emergency_stop(self):
        self.emergency_stops += 1
        self.estop_while_blocked = self.blocking.is_set()
        self.calls.append("emergency_stop")
        self.released.set()          # the arm halts -> the blocking call returns

    async def cleanup(self):
        self.cleanups += 1
        self.calls.append("cleanup")


class _RecordingRunner(gui_runner.FlowRunner):
    def __init__(self, *args, calls, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = calls

    def _cancel_task(self, task):
        self.calls.append("cancel")
        super()._cancel_task(task)


def _blocking_runner(tmp_path, calls, **kwargs):
    built = []

    def robot_factory(robot_id, *, use_picus2, ports, workspace_validator):
        robot = _BlockingRobot(calls, use_dobot=True, workspace_validator=workspace_validator)
        built.append(robot)
        return robot

    flow = {"name": "blocking stop", "steps": [
        {"action": "move_z", "robot_id": 1, "distance": 10.0},
        {"action": "move_z", "robot_id": 1, "distance": 10.0},
    ]}
    runner = _RecordingRunner(flow, mock=True, logs_dir=str(tmp_path),
                              robot_factory=robot_factory, calls=calls, **kwargs)
    return runner, built


def test_stop_emergency_stops_from_ui_thread_while_loop_is_blocked(tmp_path):
    calls = []
    runner, built = _blocking_runner(tmp_path, calls)
    runner.start()
    deadline = time.time() + 30
    while not (built and built[0].blocking.is_set()):
        assert time.time() < deadline, "the blocking step never started"
        time.sleep(0.02)

    runner.request_stop()
    assert runner.join(30), "the run did not stop"

    robot = built[0]
    # The e-stop reached the robot while the event loop was still blocked,
    # i.e. it did not wait for the task cancellation to land.
    assert robot.estop_while_blocked
    assert calls.index("emergency_stop") < calls.index("cancel")
    # Session did not send a second e-stop to the already-stopped robot.
    assert robot.emergency_stops == 1
    assert robot.move_z_calls == 1, "no further step may run after Stop"
    assert robot.cleanups == 1
    assert runner.status == "aborted"


def test_second_stop_click_is_a_noop(tmp_path):
    calls = []
    runner, built = _blocking_runner(tmp_path, calls)
    runner.start()
    deadline = time.time() + 30
    while not (built and built[0].blocking.is_set()):
        assert time.time() < deadline
        time.sleep(0.02)

    runner.request_stop()
    runner.request_stop()          # double click
    runner.request_stop()
    assert runner.join(30)
    assert calls.count("cancel") == 1
    assert built[0].emergency_stops == 1
    assert built[0].cleanups == 1
    assert runner.status == "aborted"


def test_stop_during_pre_step_prevents_the_step_dispatch(tmp_path):
    """Stop pressed while pre_step blocks (HTTP safety gate): the step is not run."""
    calls = []
    holder = {}

    def pre_step(step):
        holder["runner"].request_stop()   # the UI thread would do this meanwhile

    runner, built = _blocking_runner(tmp_path, calls, pre_step=pre_step)
    holder["runner"] = runner
    runner.start()
    assert runner.join(30)
    assert built and built[0].move_z_calls == 0
    assert built[0].emergency_stops == 1
    assert built[0].cleanups == 1
    assert runner.status == "aborted"


def test_stop_after_the_run_finished_does_not_touch_the_hardware(tmp_path):
    calls = []
    runner, built = _blocking_runner(tmp_path, calls)
    runner.steps = [{"action": "wait", "robot_id": 1, "seconds": 0.0}]
    runner.start()
    assert runner.join(30)
    assert runner.status == "completed", runner.error
    runner.request_stop()
    assert built[0].emergency_stops == 0


# ======================================================================
# 2. ExperimentSession recovery path
# ======================================================================
class FakeRobot:
    def __init__(self, calls, name, go_home_exc=None, cleanup_exc=None):
        self.calls, self.name = calls, name
        self.go_home_exc, self.cleanup_exc = go_home_exc, cleanup_exc

    def emergency_stop(self):
        self.calls.append(f"{self.name}:emergency_stop")

    async def go_home(self):
        self.calls.append(f"{self.name}:go_home")
        if self.go_home_exc is not None:
            raise self.go_home_exc

    async def cleanup(self):
        self.calls.append(f"{self.name}:cleanup")
        if self.cleanup_exc is not None:
            raise self.cleanup_exc


class FakeShared:
    def __init__(self, calls):
        self.calls = calls

    async def cleanup(self):
        self.calls.append("shared:cleanup")


def _session(calls, r1=None, r2=None):
    session = ExperimentSession()
    session.robots[1] = r1 or FakeRobot(calls, "r1")
    session.robots[2] = r2 or FakeRobot(calls, "r2")
    session.shared = FakeShared(calls)
    return session


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
def test_interrupt_during_recovery_homing_emergency_stops(interrupt):
    calls = []
    session = _session(calls, r1=FakeRobot(calls, "r1", go_home_exc=interrupt()))

    async def body():
        raise ValueError("step failed")

    with pytest.raises(interrupt):
        asyncio.run(session.run(body))

    assert "r1:emergency_stop" in calls and "r2:emergency_stop" in calls
    assert calls.index("r1:emergency_stop") < calls.index("r1:cleanup")
    assert "r2:go_home" not in calls, "homing must stop at the interruption"
    assert {"r1:cleanup", "r2:cleanup", "shared:cleanup"} <= set(calls)
    assert session.status == "aborted"
    assert "step failed" in session.error


def test_cleanup_survives_cancellation_of_one_device():
    calls = []
    session = _session(calls, r1=FakeRobot(calls, "r1", cleanup_exc=asyncio.CancelledError()))

    async def body():
        return "ok"

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.run(body))
    assert "r2:cleanup" in calls and "shared:cleanup" in calls


def test_workspace_violation_does_not_trigger_go_home_all():
    calls = []
    session = _session(calls)

    async def body():
        WorkspaceValidator().validate_xyz(999, 0, 0)

    with pytest.raises(WorkspaceViolationError):
        asyncio.run(session.run(body))
    assert not any("go_home" in c for c in calls)
    assert not any("emergency_stop" in c for c in calls)
    assert session.status == "failed"
    assert {"r1:cleanup", "r2:cleanup", "shared:cleanup"} <= set(calls)


def test_robot_interrupted_during_initialize_is_disconnected():
    cleaned = []

    class SlowInit:
        async def initialize(self):
            raise asyncio.CancelledError()

        async def cleanup(self):
            cleaned.append(True)

    session = ExperimentSession(mock=True, robot_factory=lambda *a, **k: SlowInit())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.add_robot(1))
    assert cleaned == [True]
    assert session.robots == {}


# ======================================================================
# 3. LabRobot: no auto-homing on a rejected move; validated go_home
# ======================================================================
class FakeDobot:
    """PyDobotController stand-in that records motion commands."""

    speed_preset = "中速"

    def __init__(self, pose=(200.0, 0.0, 50.0, 0.0, 0.0), fail=False):
        x, y, z, r, j1 = pose
        self.pose = [x, y, z, r, j1, 0.0, 0.0, 0.0]
        self.fail = fail
        self.commands = []
        self.home_params = None

    def get_current_position(self):
        return list(self.pose)

    def _cmd(self, *c):
        self.commands.append(c)
        if self.fail:
            raise IOError("serial error")

    def move_Z(self, dz):
        self._cmd("Z", dz)

    def move_angle(self, a):
        self._cmd("J1", a)

    def move_XYZ_abs(self, x, y, z):
        self._cmd("XYZ", x, y, z)

    def set_speed_preset(self, p):
        pass

    def set_home_params(self, *a):
        self.home_params = a
        return True


def _lab_robot(dobot, home=None):
    robot = LabRobot(use_dobot=True, workspace_validator=WorkspaceValidator())
    robot.dobot = dobot
    for attr in ("wait_after_angle_move", "wait_after_z_move", "wait_after_xy_move"):
        setattr(robot, attr, 0)
    robot.home_position = home if home is not None else [200.0, 0.0, 100.0, 0.0]
    return robot


@pytest.mark.parametrize("method, args", [
    ("move_z", (500.0,)),
    ("rotate_relative", (500.0,)),
    ("rotate", (500.0,)),
    ("move_radial", (500.0,)),
    ("move_xyz", (999.0, 0.0, 0.0)),
])
def test_rejected_move_does_not_auto_home(method, args):
    dobot = FakeDobot()
    robot = _lab_robot(dobot)
    homed = []

    async def fake_go_home():
        homed.append(True)

    robot.go_home = fake_go_home
    with pytest.raises(WorkspaceViolationError):
        asyncio.run(getattr(robot, method)(*args))
    assert homed == []
    assert dobot.commands == []


def test_device_error_still_homes_and_keeps_the_original_error():
    dobot = FakeDobot(fail=True)
    robot = _lab_robot(dobot)
    with pytest.raises(IOError, match="serial error"):
        asyncio.run(robot.move_z(-10.0))
    # the failed move, then the (validated) recovery homing was attempted
    assert dobot.commands[0] == ("Z", -10.0)
    assert len(dobot.commands) >= 2


def test_go_home_rejects_a_home_outside_the_limits():
    dobot = FakeDobot()
    robot = _lab_robot(dobot, home=[200.0, 0.0, 400.0, 0.0])   # z above z_max
    with pytest.raises(WorkspaceViolationError):
        asyncio.run(robot.go_home())
    assert dobot.commands == [], "no leg may be commanded before all are validated"


def test_go_home_rejects_a_home_joint1_outside_the_limits():
    dobot = FakeDobot(pose=(200.0, 0.0, 20.0, 0.0, 0.0))
    robot = _lab_robot(dobot, home=[200.0, 0.0, 100.0, 170.0])
    with pytest.raises(WorkspaceViolationError):
        asyncio.run(robot.go_home())
    assert dobot.commands == [], "the Z lift must not run when a later leg is invalid"


def test_go_home_within_limits_commands_legs_in_order():
    dobot = FakeDobot(pose=(200.0, 0.0, 20.0, 0.0, 10.0))
    robot = _lab_robot(dobot, home=[200.0, 0.0, 100.0, 0.0])
    asyncio.run(robot.go_home())
    assert [c[0] for c in dobot.commands] == ["Z", "J1", "XYZ"]


def test_captured_home_outside_limits_warns_and_is_not_accepted(caplog):
    dobot = FakeDobot(pose=(200.0, 0.0, 400.0, 0.0, 0.0))
    robot = _lab_robot(dobot)
    with caplog.at_level(logging.WARNING, logger="src.devices.safety.lab_robot"):
        robot.set_current_position_as_home()
    assert any("可動域外" in r.getMessage() for r in caplog.records)
    assert dobot.home_params is None, "an out-of-limits home must not reach the firmware"
    assert robot.home_is_within_limits() is False
    with pytest.raises(WorkspaceViolationError):
        asyncio.run(robot.go_home())
    assert dobot.commands == []


def test_captured_home_within_limits_is_accepted():
    dobot = FakeDobot(pose=(200.0, 0.0, 50.0, 0.0, 0.0))
    robot = _lab_robot(dobot)
    robot.set_current_position_as_home()
    assert dobot.home_params is not None
    assert robot.home_is_within_limits() is True


def test_lab_robot_cleanup_survives_cancellation():
    class Dev:
        def __init__(self, exc=None):
            self.exc, self.closed = exc, False

        def disconnect(self):
            self.closed = True
            if self.exc:
                raise self.exc

    robot = LabRobot()
    robot.dobot = Dev(asyncio.CancelledError())
    robot.ika = Dev()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(robot.cleanup())
    assert robot.ika.closed


def test_mock_go_home_rejects_a_home_outside_the_limits():
    robot = MockLabRobot(use_dobot=True, workspace_validator=WorkspaceValidator())
    robot.home_position = [200.0, 0.0, 400.0, 0.0]
    with pytest.raises(WorkspaceViolationError):
        asyncio.run(robot.go_home())
