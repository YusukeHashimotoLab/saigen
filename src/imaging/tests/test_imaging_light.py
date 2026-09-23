"""Item 4: BLE light error handling, device choice and clean-up (no Bluetooth).

Only pure helpers and fake transports are exercised; no BLE stack is
touched. The Windows/bleak backend classes are tested with fake
winrt/bleak objects on non-macOS platforms.
"""
import asyncio
import sys
import threading
import types

import pytest

import acquire
import neewer_light as nl
from helpers_imaging import FakeLight


def test_pick_device_single_multiple_and_id():
    a = {"id": "BluetoothLE#aa:bb", "name": "NEEWER-RGB62"}
    b = {"id": "BluetoothLE#cc:dd", "name": "NEEWER-RGB62"}
    assert nl.pick_device([a]) is a
    with pytest.raises(nl.AmbiguousDeviceError, match="NEEWER_DEVICE_ID"):
        nl.pick_device([a, b])
    assert nl.pick_device([a, b], "CC:DD") is b
    with pytest.raises(RuntimeError, match="No NEEWER light found"):
        nl.pick_device([a, b], "ee:ff")
    with pytest.raises(RuntimeError):
        nl.pick_device([])


def test_select_write_characteristic_mode_matches_choice():
    # the old Windows loop kept WRITE_WITHOUT_RESPONSE when a later
    # characteristic supported WRITE; the mode must follow the choice
    chars = [("wnr", False, True), ("w", True, False)]
    assert nl.select_write_characteristic(chars) == ("w", True)
    assert nl.select_write_characteristic([("wnr", False, True)]) == ("wnr", False)
    with pytest.raises(RuntimeError, match="No writable"):
        nl.select_write_characteristic([("ro", False, False)])


def test_wait_write_ack_error_and_timeout():
    ev = threading.Event()
    with pytest.raises(TimeoutError):
        nl.wait_write_ack(ev, lambda: None, timeout=0.01)
    ev.set()
    with pytest.raises(RuntimeError, match="BLE write failed"):
        nl.wait_write_ack(ev, lambda: "GATT error 3")
    nl.wait_write_ack(ev, lambda: None)              # ok


def test_restore_light_runs_power_off_and_close_when_cct_fails():
    light = FakeLight(fail_cct=True)
    with pytest.raises(RuntimeError, match="BLE write failed"):
        acquire.restore_light(light, (0, 5600))
    assert ("power", False) in light.calls and light.calls[-1] == ("close",)


def test_restore_light_closes_when_restore_cct_fails():
    light = FakeLight(fail_cct=True)
    with pytest.raises(RuntimeError):
        acquire.restore_light(light, (20, 5600))
    assert light.calls[-1] == ("close",)


def test_restore_light_normal():
    light = FakeLight()
    assert acquire.restore_light(light, (0, 5600)) == "off"
    assert light.calls == [("cct", 1, 5600), ("power", False), ("close",)]


non_mac = pytest.mark.skipif(sys.platform == "darwin",
                             reason="asyncio/bleak backends are not used on macOS")


@non_mac
def test_run_timeout_cancels_future():
    class Loop(nl._AsyncLoopMixin):
        pass
    lp = Loop()
    lp._start_loop(0.0)
    started, cancelled = threading.Event(), threading.Event()

    async def slow():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
    try:
        with pytest.raises(TimeoutError):
            lp._run(slow(), timeout=0.2)
        assert cancelled.wait(2), "timed-out coroutine kept running"
    finally:
        lp._stop_loop()


@non_mac
def test_winrt_constructor_cleans_up_partial_connection(monkeypatch):
    closed = []

    class Dev:
        def close(self):
            closed.append(True)

    async def failing_connect(self):
        self._dev = Dev()
        raise RuntimeError("Cannot enumerate characteristics")
    monkeypatch.setattr(nl._WinConnectedNeewer, "_connect", failing_connect)
    with pytest.raises(RuntimeError, match="enumerate"):
        nl._WinConnectedNeewer(timeout=1.0)
    assert closed == [True]


@non_mac
def test_win_factory_does_not_fall_back_on_ambiguity(monkeypatch):
    def ambiguous(timeout):
        raise nl.AmbiguousDeviceError("two lights")
    monkeypatch.setattr(nl, "_WinConnectedNeewer", ambiguous)
    monkeypatch.setattr(nl, "_BleakNeewer",
                        lambda t: pytest.fail("must not fall back to scanning"))
    with pytest.raises(nl.AmbiguousDeviceError):
        nl._win_neewer(1.0)


def _fake_bleak(monkeypatch, devices, client_log):
    class Dev:
        def __init__(self, addr, name):
            self.address, self.name = addr, name

    class Adv:
        local_name, service_uuids = None, []

    class Scanner:
        @staticmethod
        async def discover(timeout, return_adv):
            return {a: (Dev(a, n), Adv()) for a, n in devices}

    class Ch:
        def __init__(self, props):
            self.properties = props

    class Svc:
        characteristics = [Ch(["write-without-response"]), Ch(["write"])]

    class Services(list):
        def get_service(self, uuid):
            return Svc()

    class Client:
        def __init__(self, dev):
            self.dev, self.is_connected = dev, False
            self.services = Services([Svc()])
            client_log.append(self)

        async def connect(self):
            self.is_connected = True

        async def disconnect(self):
            client_log.append("disconnect")
            self.is_connected = False

        async def write_gatt_char(self, ch, data, response):
            client_log.append(("write", tuple(ch.properties), response))

    monkeypatch.setitem(sys.modules, "bleak",
                        types.SimpleNamespace(BleakScanner=Scanner, BleakClient=Client))


@non_mac
def test_bleak_rejects_multiple_lights(monkeypatch):
    log = []
    _fake_bleak(monkeypatch, [("AA", "NEEWER-1"), ("BB", "NEEWER-2")], log)
    monkeypatch.setattr(nl, "_device_id", lambda: None)
    with pytest.raises(nl.AmbiguousDeviceError):
        nl._BleakNeewer(timeout=1.0)
    assert log == []                                 # never connected


@non_mac
def test_bleak_device_id_selects_and_write_mode_follows_char(monkeypatch):
    log = []
    _fake_bleak(monkeypatch, [("AA", "NEEWER-1"), ("BB", "NEEWER-2")], log)
    monkeypatch.setattr(nl, "_device_id", lambda: "bb")
    light = nl._BleakNeewer(timeout=1.0)
    try:
        assert log[0].dev.address == "BB"
        light.power(True)
        assert log[-1] == ("write", ("write",), True)
    finally:
        light.close()
    assert "disconnect" in log


@non_mac
def test_bleak_constructor_disconnects_on_failure(monkeypatch):
    log = []
    _fake_bleak(monkeypatch, [("AA", "NEEWER-1")], log)
    monkeypatch.setattr(nl, "_device_id", lambda: None)

    def no_char(self):
        raise RuntimeError("No writable characteristic found")
    monkeypatch.setattr(nl._BleakNeewer, "_find_write_char", no_char)
    with pytest.raises(RuntimeError, match="No writable"):
        nl._BleakNeewer(timeout=1.0)
    assert "disconnect" in log
