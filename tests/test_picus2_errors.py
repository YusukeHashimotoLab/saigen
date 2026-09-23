"""Picus 2: a failed trigger/command must propagate, never be logged as completed.

Previously ``Picus2Controller.button()`` swallowed ``serial.SerialException``
(not a ``ConnectionError`` subclass) and Bleak errors, so a failed aspirate or
dispense trigger returned normally and ``LabRobot`` updated its held volume as
if liquid had moved. No hardware is touched: serial/bleak are stubbed.
"""
import asyncio
import sys

import pytest

import tests.test_wp2_picus2_connection  # noqa: F401  installs serial/bleak stubs

from src.devices.picus2.picus2_controller import (  # noqa: E402
    Buttons,
    ConnectionType,
    Picus2CommandError,
    Picus2Controller,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)


class _SerialException(Exception):
    """Stands in for serial.SerialException: deliberately NOT a ConnectionError."""


class _FlakySerial:
    """Open port whose write() fails on the n-th call (1-based)."""

    def __init__(self, fail_on=1, exc=None):
        self.is_open = True
        self.calls = 0
        self.fail_on = fail_on
        self.exc = exc or _SerialException("write failed: device disconnected")
        self.closed = False

    def flush(self):
        pass

    def write(self, data):
        self.calls += 1
        if self.calls == self.fail_on:
            raise self.exc
        return len(data)

    def readline(self):
        return b"END\r\n"

    def close(self):
        self.is_open = False
        self.closed = True


def _usb(serial_obj, motor=True):
    p = Picus2Controller("COM4")
    p.serial = serial_obj
    p.motor_mode = motor
    return p


def test_command_error_is_a_connection_error():
    assert issubclass(Picus2CommandError, ConnectionError)
    assert not issubclass(_SerialException, ConnectionError)


def test_button_propagates_serial_exception():
    p = _usb(_FlakySerial(fail_on=1))
    with pytest.raises(Picus2CommandError) as exc:
        asyncio.run(p.button(Buttons.TRIGGER_BUTTON_TOP))
    assert isinstance(exc.value.__cause__, _SerialException)
    assert "COM4" in str(exc.value)


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.aspirate(1.0, speed=5),
        lambda p: p.dispense(1.0, speed=5),
        lambda p: p.blow_out(delay_ms=0),
        lambda p: p.mix(2, 5, 1.0),
        lambda p: p.eject_tip(),
    ],
)
def test_failed_trigger_fails_the_liquid_operation(call):
    """Command is accepted (write #1) but the trigger button (write #2) fails."""
    p = _usb(_FlakySerial(fail_on=2))
    with pytest.raises(Picus2CommandError):
        asyncio.run(call(p))


def test_failed_operation_command_propagates_as_connection_error():
    p = _usb(_FlakySerial(fail_on=1))
    with pytest.raises(ConnectionError):
        asyncio.run(p.aspirate(1.0, speed=5))


def test_set_motor_mode_trigger_failure_propagates():
    p = _usb(_FlakySerial(fail_on=2), motor=False)
    with pytest.raises(Picus2CommandError):
        asyncio.run(p.set_motor_mode(True))


def test_success_path_unchanged():
    s = _FlakySerial(fail_on=0)  # never fails
    p = _usb(s)
    assert asyncio.run(p.button(Buttons.TRIGGER_BUTTON_TOP)) is None
    assert asyncio.run(p.aspirate(1.0, speed=5)) is None
    assert s.calls == 3


def test_send_on_closed_usb_port_raises_instead_of_noop():
    p = Picus2Controller("COM4")
    s = _FlakySerial(fail_on=0)
    s.is_open = False
    p.serial = s
    with pytest.raises(Picus2CommandError):
        asyncio.run(p.send_command('{"button": "TRIGGER_BUTTON_TOP"}'))


def test_usb_read_error_while_waiting_for_end_propagates():
    class _BadRead(_FlakySerial):
        def readline(self):
            raise _SerialException("read failed")

    p = _usb(_BadRead(fail_on=0))
    with pytest.raises(Picus2CommandError):
        asyncio.run(p.eject_tip())


class _BleakError(Exception):
    """Stands in for bleak.exc.BleakError."""


class _FailingGattClient:
    is_connected = True

    async def write_gatt_char(self, uuid, data):
        raise _BleakError("GATT write failed")


def test_bluetooth_trigger_failure_propagates():
    p = Picus2Controller("AA:BB:CC:DD:EE:FF", connection_type=ConnectionType.BLUETOOTH)
    p.client = _FailingGattClient()
    p.motor_mode = True
    with pytest.raises(Picus2CommandError) as exc:
        asyncio.run(p.button(Buttons.TRIGGER_BUTTON_TOP))
    assert isinstance(exc.value.__cause__, _BleakError)
    with pytest.raises(Picus2CommandError):
        asyncio.run(p.dispense(1.0, speed=5))


def test_usb_connect_closes_port_when_setup_fails(monkeypatch):
    """Port object created, then a later setup step raises: the handle is closed."""
    created = []

    class _HalfOpen:
        def __init__(self, *a, **kw):
            self.closed = False
            created.append(self)

        @property
        def is_open(self):
            raise _SerialException("port state unavailable")

        def close(self):
            self.closed = True

    monkeypatch.setattr(sys.modules["serial"], "Serial", _HalfOpen, raising=False)
    p = Picus2Controller("COM4")
    with pytest.raises(_SerialException):
        asyncio.run(p.connect())
    assert created and created[0].closed
    assert p.serial is None


def test_usb_connect_closes_port_reported_not_open(monkeypatch):
    created = []

    class _NotOpen:
        def __init__(self, *a, **kw):
            self.is_open = False
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(sys.modules["serial"], "Serial", _NotOpen, raising=False)
    p = Picus2Controller("COM4")
    with pytest.raises(ConnectionError):
        asyncio.run(p.connect())
    assert created[0].closed
    assert p.serial is None


def test_lab_robot_held_volume_not_updated_when_trigger_fails():
    """End to end with the (unmodified) safety wrapper: failed aspirate keeps volume at 0."""
    from src.devices.safety.lab_robot import LabRobot

    robot = LabRobot(use_dobot=False, use_picus2=True, use_ika=False)
    robot.picus2 = _usb(_FlakySerial(fail_on=2))
    before = robot.pipette_volume
    with pytest.raises(Exception):
        asyncio.run(robot.aspirate(1.0, speed=5))
    assert robot.pipette_volume == before
