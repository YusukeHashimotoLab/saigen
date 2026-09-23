"""ExperimentSession: SafetyAbort handling and no homing of e-stopped robots."""
import asyncio

import pytest

from src.flow.experiment_session import ExperimentSession, SafetyAbort


class FakeRobot:
    def __init__(self, calls, name):
        self.calls = calls
        self.name = name

    def emergency_stop(self):
        self.calls.append(f"{self.name}:emergency_stop")

    async def go_home(self):
        self.calls.append(f"{self.name}:go_home")

    async def cleanup(self):
        self.calls.append(f"{self.name}:cleanup")


def _session(calls):
    session = ExperimentSession(workspace_validator=object())
    session.robots[1] = FakeRobot(calls, "r1")
    session.robots[2] = FakeRobot(calls, "r2")
    return session


def test_safety_abort_emergency_stops_and_does_not_go_home():
    calls = []
    session = _session(calls)

    async def body():
        raise SafetyAbort("humidity interlock tripped")

    with pytest.raises(SafetyAbort):
        asyncio.run(session.run(body))

    assert session.status == "aborted"
    assert "humidity interlock tripped" in session.error
    assert "r1:emergency_stop" in calls and "r2:emergency_stop" in calls
    assert not any("go_home" in c for c in calls)
    assert calls.index("r1:emergency_stop") < calls.index("r1:cleanup")
    assert "r2:cleanup" in calls


def test_safety_abort_after_a_gui_stop_still_stops_every_robot():
    calls = []
    session = _session(calls)
    session.emergency_stop_all()          # e.g. the GUI Stop, already sent
    calls.clear()

    async def body():
        raise SafetyAbort("gate")

    with pytest.raises(SafetyAbort):
        asyncio.run(session.run(body))
    assert calls.count("r1:emergency_stop") == 1
    assert calls.count("r2:emergency_stop") == 1


def test_plain_error_still_homes_robots():
    calls = []
    session = _session(calls)

    async def body():
        raise RuntimeError("pipette error")

    with pytest.raises(RuntimeError):
        asyncio.run(session.run(body))
    assert session.status == "failed"
    assert "r1:go_home" in calls and "r2:go_home" in calls


def test_go_home_all_skips_emergency_stopped_robots():
    calls = []
    session = _session(calls)
    session.robots[1].emergency_stop = lambda: calls.append("r1:emergency_stop")
    session._estopped_ids.add(1)
    asyncio.run(session.go_home_all())
    assert "r1:go_home" not in calls
    assert "r2:go_home" in calls
