"""MockLabRobot parity with LabRobot, so a clean --mock run predicts the real run.

(a) held pipette volume with the same limits and exception types,
(b) a coherent simulated pose (rotation moves X/Y, XYZ moves update Joint1,
    radial moves follow the current radius and reject the base singularity),
(c) a configurable start pose (``start_pose`` / ``mock_start_pose`` /
    ``MOCK_START_POSE``) from which relative moves are validated.
"""
import asyncio
import math

import pytest

from src.devices.safety.lab_robot import LabRobot
from src.devices.safety.mock_robot import MockLabRobot, parse_start_pose
from src.devices.safety.validators import WorkspaceValidator, WorkspaceViolationError
from src.flow import experiment_session
from src.flow.experiment_session import ExperimentSession, mock_robot_factory, mock_start_pose


# ----------------------------------------------------------------------
# (a) pipette volume: same limits, same exception types as LabRobot
# ----------------------------------------------------------------------
class _FakePicus:
    async def set_motor_mode(self, mode):
        pass

    async def aspirate(self, v, speed=5):
        pass

    async def dispense(self, v, speed=5):
        pass

    async def blow_out(self, go_home=True, speed=1, delay_ms=3000):
        pass


def _real():
    robot = LabRobot(use_picus2=True)
    robot.picus2 = _FakePicus()
    robot.wait_after_pipette = 0
    return robot


def _mock():
    return MockLabRobot(use_picus2=True)


SCENARIOS = [
    # (name, steps, expected exception of the last step or None)
    ("over capacity", [("aspirate", 6.0), ("aspirate", 5.0)], ValueError),
    ("below minimum", [("aspirate", 0.2)], ValueError),
    ("dispense more than held", [("aspirate", 2.0), ("dispense", 3.0)], ValueError),
    ("dispense without aspirate", [("dispense", 1.0)], ValueError),
    ("bad speed", [("aspirate", 1.0, 10)], ValueError),
    ("blow_out resets", [("aspirate", 8.0), ("blow_out",), ("aspirate", 8.0)], None),
    ("exact capacity", [("aspirate", 5.0), ("aspirate", 5.0), ("dispense", 10.0)], None),
]


async def _play(robot, steps):
    for step in steps:
        name, *args = step
        await getattr(robot, name)(*args)


@pytest.mark.parametrize("name, steps, exc", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_mock_pipette_limits_match_lab_robot(name, steps, exc, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    outcomes = []
    for robot in (_real(), _mock()):
        try:
            asyncio.run(_play(robot, steps))
            outcomes.append((None, robot.pipette_volume))
        except Exception as e:  # noqa: BLE001
            outcomes.append((type(e), robot.pipette_volume))
    assert outcomes[0] == outcomes[1], outcomes
    assert outcomes[0][0] is exc


_real_sleep = asyncio.sleep


async def _instant_sleep(delay, result=None):
    await _real_sleep(0)
    return result


def test_mock_without_picus2_refuses_liquid_ops_like_lab_robot():
    for robot in (LabRobot(use_picus2=False), MockLabRobot(use_picus2=False)):
        with pytest.raises(RuntimeError):
            asyncio.run(robot.aspirate(1.0))


def test_mock_cancelled_aspirate_makes_the_volume_unknown():
    robot = _mock()

    async def scenario():
        task = asyncio.ensure_future(robot.aspirate(1.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert robot.pipette_volume_known is False
    with pytest.raises(RuntimeError, match="保持量が不明"):
        asyncio.run(robot.aspirate(1.0))
    robot.reset_pipette_volume()
    assert robot.pipette_volume_known


# ----------------------------------------------------------------------
# (b) coherent simulated pose
# ----------------------------------------------------------------------
def test_mock_rotation_moves_xy_along_the_circle(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    robot = MockLabRobot(use_dobot=True)          # (250, 0, 150), J1 = 0
    asyncio.run(robot.rotate(90.0))
    x, y, z, j1 = robot._pos
    assert (x, y, z, j1) == pytest.approx((0.0, 250.0, 150.0, 90.0), abs=1e-6)
    asyncio.run(robot.rotate_relative(-45.0))
    x, y, _, j1 = robot._pos
    assert j1 == pytest.approx(45.0)
    assert math.hypot(x, y) == pytest.approx(250.0)
    assert math.degrees(math.atan2(y, x)) == pytest.approx(45.0)


def test_mock_move_xyz_updates_joint1(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    robot = MockLabRobot(use_dobot=True)
    asyncio.run(robot.move_xyz(100.0, 100.0, 50.0))
    assert robot._pos[3] == pytest.approx(45.0)
    # a relative rotation is now validated from J1 = 45, like the real arm
    robot.workspace_validator = WorkspaceValidator(joint1_min=-135, joint1_max=135)
    with pytest.raises(WorkspaceViolationError):
        asyncio.run(robot.rotate_relative(100.0))


def test_mock_radial_follows_the_current_radius_even_without_validator(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    robot = MockLabRobot(use_dobot=True)
    asyncio.run(robot.rotate(90.0))
    asyncio.run(robot.move_radial(-50.0))
    assert robot._pos[0] == pytest.approx(0.0, abs=1e-6)
    assert robot._pos[1] == pytest.approx(200.0)


@pytest.mark.parametrize("validator", [None, WorkspaceValidator()])
def test_mock_radial_near_the_base_raises_like_lab_robot(validator):
    robot = MockLabRobot(use_dobot=True, workspace_validator=validator,
                         start_pose=(0.5, 0.0, 100.0, 0.0))
    with pytest.raises(ValueError, match="半径距離が小さすぎます"):
        asyncio.run(robot.move_radial(10.0))


def test_mock_get_current_position_uses_the_driver_layout():
    robot = MockLabRobot(start_pose=(0.0, 200.0, 10.0, 90.0))
    pose = robot.get_current_position()
    assert pose[:3] == [0.0, 200.0, 10.0]
    assert pose[4] == pytest.approx(90.0)


# ----------------------------------------------------------------------
# (c) start pose
# ----------------------------------------------------------------------
def test_default_start_pose_is_unchanged():
    robot = MockLabRobot()
    assert robot._pos == [250.0, 0.0, 150.0, 0.0]
    assert robot.home_position == [250.0, 0.0, 150.0, 0.0]


def test_start_pose_changes_relative_validation(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    v = WorkspaceValidator(z_min=-130, z_max=150)
    # from the default pose (z = 150) any upward move is rejected ...
    with pytest.raises(WorkspaceViolationError):
        asyncio.run(MockLabRobot(workspace_validator=v).move_z(100.0))
    # ... but from the pose the real run will start at (z = 0) it is fine
    robot = MockLabRobot(workspace_validator=v, start_pose="200,0,0,0")
    asyncio.run(robot.move_z(100.0))
    assert robot._pos[2] == pytest.approx(100.0)


@pytest.mark.parametrize("value, expected", [
    ("200,10,50,3", (200.0, 10.0, 50.0, 3.0)),
    (" 200, 10 , 50, 3 ", (200.0, 10.0, 50.0, 3.0)),
    ([200, 10, 50, 3], (200.0, 10.0, 50.0, 3.0)),
    (None, None),
    ("", None),
])
def test_parse_start_pose(value, expected):
    assert parse_start_pose(value) == expected


@pytest.mark.parametrize("bad", ["1,2,3", "a,b,c,d", [1, 2, 3, 4, 5], "1,2,3,nan"])
def test_parse_start_pose_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        parse_start_pose(bad)


def test_factory_reads_mock_start_pose_from_env(monkeypatch):
    monkeypatch.setenv("MOCK_START_POSE", "180,20,40,6.34")
    robot = mock_robot_factory(1, use_picus2=False, ports={}, workspace_validator=None)
    assert robot._pos[:3] == [180.0, 20.0, 40.0]


def test_factory_reads_mock_start_pose_from_shared_config(monkeypatch):
    monkeypatch.delenv("MOCK_START_POSE", raising=False)
    monkeypatch.setattr(experiment_session.lab_config, "get_shared_devices",
                        lambda: {"mock_start_pose": [150, 0, 30, 0]})
    robot = mock_robot_factory(1, use_picus2=False, ports={}, workspace_validator=None)
    assert robot._pos == [150.0, 0.0, 30.0, 0.0]


def test_env_overrides_shared_config(monkeypatch):
    monkeypatch.setenv("MOCK_START_POSE", "100,0,10,0")
    assert mock_start_pose(1, {"mock_start_pose": "150,0,30,0"}) == (100.0, 0.0, 10.0, 0.0)


def test_per_robot_start_pose_mapping(monkeypatch):
    monkeypatch.delenv("MOCK_START_POSE", raising=False)
    cfg = {"mock_start_pose": {1: "150,0,30,0", "2": [0, 200, 20, 90]}}
    assert mock_start_pose(1, cfg) == (150.0, 0.0, 30.0, 0.0)
    assert mock_start_pose(2, cfg) == (0.0, 200.0, 20.0, 90.0)
    assert mock_start_pose(3, cfg) is None


def test_invalid_start_pose_fails_loudly(monkeypatch):
    monkeypatch.setenv("MOCK_START_POSE", "1,2")
    with pytest.raises(ValueError):
        mock_robot_factory(1, use_picus2=False, ports={}, workspace_validator=None)


def test_session_home_is_the_start_pose(monkeypatch):
    monkeypatch.setenv("MOCK_START_POSE", "180,0,40,0")
    session = ExperimentSession(mock=True, workspace_validator=WorkspaceValidator())
    robot = asyncio.run(session.add_robot(1))
    assert robot.home_position == [180.0, 0.0, 40.0, 0.0]
