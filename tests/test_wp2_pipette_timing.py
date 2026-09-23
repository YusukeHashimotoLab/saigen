"""Pipette timing: ピペット公称所要時間（マニュアル p.63 の表）をログ／記録に載せる。"""
import asyncio
import logging

import pytest

from tests.test_wp2_picus2_connection import _install_stubs  # serial/bleak スタブ

_install_stubs()

from src.devices.picus2.picus2_controller import Picus2Controller  # noqa: E402


@pytest.mark.parametrize("speed,expected", [(1, 5.1), (5, 1.45), (9, 0.45)])
def test_calculate_operation_time_table(speed, expected):
    picus = Picus2Controller("COM4")
    assert picus.calculate_operation_time(5.0, speed) == pytest.approx(expected)


class _FakePicus:
    def __init__(self):
        self.calls = []

    async def set_motor_mode(self, mode):
        pass

    async def aspirate(self, volume, speed=5):
        self.calls.append(("aspirate", volume, speed))

    async def dispense(self, volume, speed=5):
        self.calls.append(("dispense", volume, speed))

    def calculate_operation_time(self, amount, speed):
        return Picus2Controller.calculate_operation_time(self, amount, speed)

    DEBUG = False

    def debug_print(self, msg):
        pass


def _lab_robot_with_fake_picus():
    from src.devices.safety.lab_robot import LabRobot

    robot = LabRobot(use_dobot=False, use_picus2=True, use_ika=False)
    robot.picus2 = _FakePicus()
    robot.wait_after_pipette = 0
    return robot


def test_lab_robot_aspirate_logs_and_returns_nominal_time(caplog):
    robot = _lab_robot_with_fake_picus()
    with caplog.at_level(logging.INFO):
        result = asyncio.run(robot.aspirate(5.0, speed=5))
    assert result is not None
    assert result["nominal_time_s"] == pytest.approx(1.45)
    assert any("1.45" in r.getMessage() for r in caplog.records), caplog.text


def test_lab_robot_dispense_returns_nominal_time():
    robot = _lab_robot_with_fake_picus()
    robot._current_pipette_volume = 5.0
    result = asyncio.run(robot.dispense(5.0, speed=1))
    assert result["nominal_time_s"] == pytest.approx(5.1)


def test_mock_robot_reports_nominal_time():
    from src.devices.safety.mock_robot import MockLabRobot

    robot = MockLabRobot(use_picus2=True)
    res = asyncio.run(robot.aspirate(5.0, speed=9))
    assert res["nominal_time_s"] == pytest.approx(0.45)


def test_executor_propagates_nominal_time():
    from src.flow.executor import execute_step

    from src.devices.safety.mock_robot import MockLabRobot

    robot = MockLabRobot(use_picus2=True)
    res = asyncio.run(
        execute_step({"action": "aspirate", "volume": 5.0, "speed": 5}, robot)
    )
    assert res["nominal_time_s"] == pytest.approx(1.45)


def test_experiment_logger_records_nominal_time(tmp_path):
    import csv

    from src.flow.experiment_logger import ExperimentLogger

    src = tmp_path / "flow.json"
    src.write_text("{}", encoding="utf-8")
    log = ExperimentLogger("wp2", "nominal time", str(src), base_dir=str(tmp_path))
    log.record_step(1, 1, "aspirate", 1, None, "ok", 1.5,
                    result={"nominal_time_s": 1.45})
    log.finalize("ok")

    with open(log.csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["nominal_time_s"] == "1.45"
