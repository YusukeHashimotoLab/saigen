"""The schema's pipette volume range matches the real LabRobot wrapper, so a
flow that the mock accepts is not rejected on hardware."""
import glob
import json
import os

import pytest
from pydantic import ValidationError

from src.devices.safety.lab_robot import LabRobot
from src.flow.schema import PIPETTE_MAX_VOLUME_ML, PIPETTE_MIN_VOLUME_ML, ExperimentWorkflow

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_schema_limits_equal_the_real_wrapper():
    robot = LabRobot(use_dobot=False, use_picus2=False)
    assert PIPETTE_MIN_VOLUME_ML == robot.min_pipette_volume
    assert PIPETTE_MAX_VOLUME_ML == robot.max_pipette_volume


@pytest.mark.parametrize("action", ["aspirate", "dispense"])
@pytest.mark.parametrize("volume", [0.01, 0.1, 0.49])
def test_volume_below_the_minimum_is_rejected(action, volume):
    with pytest.raises(ValidationError):
        ExperimentWorkflow(name="t", steps=[
            {"action": action, "robot_id": 1, "volume": volume, "speed": 5}])


@pytest.mark.parametrize("action", ["aspirate", "dispense"])
@pytest.mark.parametrize("volume", [0.5, 5.0, 10.0])
def test_volume_in_range_is_accepted(action, volume):
    ExperimentWorkflow(name="t", steps=[
        {"action": action, "robot_id": 1, "volume": volume, "speed": 5}])


def test_every_shipped_flow_still_validates():
    paths = (glob.glob(os.path.join(REPO_ROOT, "examples", "**", "*.json"), recursive=True)
             + glob.glob(os.path.join(REPO_ROOT, "src", "agent", "presets", "*.json")))
    assert paths
    for path in paths:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        flow = data.get("template", data)
        if "steps" not in flow:
            continue
        ExperimentWorkflow(**flow)
