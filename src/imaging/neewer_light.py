#!/usr/bin/env python3
"""BLE control for the NEEWER RGB62 backlight (macOS / Windows both supported).

The protocol (command construction) is shared. Only the BLE transport is
swapped per platform:

  - macOS : CoreBluetooth. NEEWER Control Center keeps holding its own
            connection, so this module "piggybacks" onto it and can coexist
            with the official app.
  - Windows : first try WinRT to find a NEEWER "already connected to this
            PC" and piggyback on it (this coexists even while the control
            app on the PC holds the connection; a connected unit stops BLE
            advertising, so it cannot be found by scanning).
            Falls back to bleak's advertisement scan if none is found.
  - other   : bleak (BlueZ) advertisement scan only.

Usage:
    python neewer_light.py on                  # power on (does not change settings)
    python neewer_light.py off                 # power off (does not change settings)
    python neewer_light.py cct 50 5600         # 50% brightness, 5600K (neutral GM)
    python neewer_light.py cct 50 5600 30      # with GM specified (0-100, 50=neutral)

If more than one NEEWER light is reachable, the module refuses to guess:
set NEEWER_DEVICE_ID (environment or the repository's .env) to part of the
device id printed in the error message.

Note: the cct command switches the light into CCT mode. If it was being used
in HSI (RGB color) mode, the color settings will be overwritten.
Protocol source: github.com/keefo/NeewerLite Docs/Neewer-Light-Protocol.md
"""
import sys

IS_MAC = sys.platform == "darwin"

NEEWER_SVC = "69400001-B5A3-F393-E0A9-E50E24DCCA99"

# Optional: which light to use when more than one NEEWER is reachable. Set
# in the environment or in the repository's .env (never in a tracked file):
#   macOS   : the peripheral identifier UUID (CoreBluetooth)
#   Windows : part of the WinRT device id, or the Bluetooth address when
#             the bleak scan fallback is used
# Matching is a case-insensitive substring match on the device id.
DEVICE_ID_KEY = "NEEWER_DEVICE_ID"
WRITE_ACK_TIMEOUT_S = 5.0
SCAN_S = 6.0            # bleak advertisement scan duration


class AmbiguousDeviceError(RuntimeError):
    """More than one light matched; refuse rather than guess."""


def _device_id() -> str | None:
    try:
        import paths
    except ImportError:          # imported from outside src/imaging
        import os
        v = os.environ.get(DEVICE_ID_KEY)
        return v.strip() if v and v.strip() else None
    return paths.env_setting(DEVICE_ID_KEY)


def pick_device(candidates: list[dict], device_id: str | None = None) -> dict:
    """Choose exactly one light from [{"id", "name", ...}].

    With device_id, only candidates whose id contains it (case-insensitive)
    are considered. Zero matches raise RuntimeError; more than one raises
    AmbiguousDeviceError, listing the ids so the user can set
    NEEWER_DEVICE_ID.
    """
    if device_id:
        cands = [c for c in candidates
                 if device_id.lower() in str(c["id"]).lower()]
    else:
        cands = list(candidates)
    if not cands:
        extra = f" with id matching {DEVICE_ID_KEY}={device_id!r}" if device_id else ""
        seen = [f"{c['name']} ({c['id']})" for c in candidates]
        raise RuntimeError(f"No NEEWER light found{extra} (seen: {seen})")
    if len(cands) > 1:
        listing = [f"{c['name']} ({c['id']})" for c in cands]
        raise AmbiguousDeviceError(
            f"Several NEEWER lights match: {listing}. Set {DEVICE_ID_KEY} "
            f"in .env to (part of) the id of the one to use.")
    return cands[0]


def select_write_characteristic(chars: list[tuple]) -> tuple:
    """Pick the characteristic to write to from [(obj, can_write,
    can_write_without_response)].

    Returns (obj, with_response). A characteristic that supports writes
    with response is preferred (errors are then reported by the device);
    the write mode is always returned together with the chosen
    characteristic so the two can never disagree.
    """
    for obj, can_write, _can_wwr in chars:
        if can_write:
            return obj, True
    for obj, _can_write, can_wwr in chars:
        if can_wwr:
            return obj, False
    raise RuntimeError("No writable characteristic found")


def wait_write_ack(event, get_error, timeout: float = WRITE_ACK_TIMEOUT_S) -> None:
    """Wait for a write-with-response acknowledgement.

    Raises TimeoutError if none arrives and RuntimeError if the device
    reported an error (get_error() returns it, or None).
    """
    if not event.wait(timeout):
        raise TimeoutError(f"No write acknowledgement from the light within {timeout:.0f} s")
    err = get_error()
    if err:
        raise RuntimeError(f"BLE write failed: {err}")


def _cmd(tag: int, payload: bytes) -> bytes:
    body = bytes([0x78, tag, len(payload)]) + payload
    return body + bytes([sum(body) & 0xFF])


class _LightMixin:
    """Transport-independent command API. send() is implemented by each backend."""

    def send(self, data: bytes) -> None:  # pragma: no cover - implemented per backend
        raise NotImplementedError

    def power(self, on: bool) -> None:
        """Power on/off. Brightness/color settings are preserved."""
        self.send(_cmd(0x81, bytes([0x01 if on else 0x02])))

    def set_cct(self, brightness: int, kelvin: int, gm: int = 50) -> None:
        """Set brightness (0-100%) and color temperature (K) in CCT mode."""
        brt = max(0, min(100, int(brightness)))
        cct = max(0, min(255, round(kelvin / 100)))
        gm = max(0, min(100, int(gm)))
        self.send(_cmd(0x87, bytes([brt, cct, gm, 0x00, 0x00])))

    def set_hsi(self, hue: int, saturation: int, intensity: int) -> None:
        """Set color in HSI mode (hue: 0-360 deg, sat/int: 0-100)."""
        hue = max(0, min(360, int(hue)))
        self.send(_cmd(0x86, bytes([hue & 0xFF, hue >> 8,
                                    max(0, min(100, int(saturation))),
                                    max(0, min(100, int(intensity)))])))


# ============================================================ macOS backend

if IS_MAC:
    import threading

    import CoreBluetooth
    import Foundation
    import libdispatch
    import objc

    class _Delegate(Foundation.NSObject):
        def init(self):
            self = objc.super(_Delegate, self).init()
            for name in ("ready", "connected", "svc_done", "chr_done", "wrote"):
                setattr(self, name, threading.Event())
            # last error reported by each callback (None = no error)
            self.errors = {}
            return self

        def centralManagerDidUpdateState_(self, c):
            st = c.state()
            if st == 5:  # powered on
                self.ready.set()
            elif st in (2, 3, 4):  # unsupported / unauthorized / powered off
                self.errors["state"] = f"Bluetooth state {st}"
                self.ready.set()

        def centralManager_didConnectPeripheral_(self, c, p):
            self.connected.set()

        def centralManager_didFailToConnectPeripheral_error_(self, c, p, e):
            self.errors["connect"] = e or "connection failed"
            self.connected.set()

        def peripheral_didDiscoverServices_(self, p, e):
            if e:
                self.errors["svc"] = e
            self.svc_done.set()

        def peripheral_didDiscoverCharacteristicsForService_error_(self, p, s, e):
            if e:
                self.errors["chr"] = e
            self.chr_done.set()

        def peripheral_didWriteValueForCharacteristic_error_(self, p, ch, e):
            self.errors["write"] = e if e else None
            self.wrote.set()

    class _MacNeewer(_LightMixin):
        """A handle to an already-connected (or advertising) NEEWER light."""

        def __init__(self, timeout: float = 15.0):
            self._p = None
            self._mgr = None
            try:
                self._connect(timeout)
            except Exception:
                # never leave a half-open connection behind
                if self._mgr is not None and self._p is not None:
                    try:
                        self._mgr.cancelPeripheralConnection_(self._p)
                    except Exception:
                        pass
                raise

        def _wait(self, event, key: str, timeout: float, what: str) -> None:
            if not event.wait(timeout):
                raise TimeoutError(f"Timed out waiting for {what}")
            err = self._d.errors.get(key)
            if err:
                raise RuntimeError(f"{what} failed: {err}")

        def _connect(self, timeout: float) -> None:
            self._d = _Delegate.alloc().init()
            queue = libdispatch.dispatch_queue_create(b"neewer-ble", None)
            self._mgr = CoreBluetooth.CBCentralManager.alloc(
            ).initWithDelegate_queue_(self._d, queue)
            if not self._d.ready.wait(timeout) or self._d.errors.get("state"):
                raise RuntimeError(
                    f"Bluetooth is not enabled ({self._d.errors.get('state')})")
            uuids = [CoreBluetooth.CBUUID.UUIDWithString_(NEEWER_SVC)]
            periphs = self._mgr.retrieveConnectedPeripheralsWithServices_(uuids)
            cands = [{"id": str(p.identifier().UUIDString()),
                      "name": str(p.name() or ""), "obj": p}
                     for p in periphs
                     if "NEEWER" in str(p.name() or "").upper()]
            if not cands:
                raise RuntimeError(
                    "No connected NEEWER light found "
                    "(check the Control Center connection)")
            self._p = pick_device(cands, _device_id())["obj"]
            self._mgr.connectPeripheral_options_(self._p, None)
            self._wait(self._d.connected, "connect", timeout, "connecting to the light")
            self._p.setDelegate_(self._d)
            self._p.discoverServices_(None)
            self._wait(self._d.svc_done, "svc", timeout, "service discovery")
            svc = next((s for s in (self._p.services() or [])
                        if str(s.UUID().UUIDString()).upper() == NEEWER_SVC), None)
            if svc is None:
                raise RuntimeError("The light does not expose the NEEWER service")
            self._p.discoverCharacteristics_forService_(None, svc)
            self._wait(self._d.chr_done, "chr", timeout, "characteristic discovery")
            ch, with_resp = select_write_characteristic(
                [(c, bool(c.properties() & 0x08), bool(c.properties() & 0x04))
                 for c in (svc.characteristics() or [])])
            self._write_ch = ch
            # CBCharacteristicWriteWithResponse = 0, WithoutResponse = 1
            self._with_response = with_resp
            self._write_type = 0 if with_resp else 1

        @property
        def name(self) -> str:
            return str(self._p.name())

        def send(self, data: bytes) -> None:
            self._d.wrote.clear()
            self._d.errors.pop("write", None)
            self._p.writeValue_forCharacteristic_type_(
                Foundation.NSData.dataWithBytes_length_(data, len(data)),
                self._write_ch, self._write_type)
            if self._with_response:
                wait_write_ack(self._d.wrote, lambda: self._d.errors.get("write"))

        def close(self) -> None:
            self._mgr.cancelPeripheralConnection_(self._p)


# ========================================================== bleak backend

else:
    import asyncio
    import concurrent.futures
    import threading

    class _AsyncLoopMixin:
        """Runs an event loop on a dedicated thread so sync methods can wait on it."""

        def _start_loop(self, timeout: float):
            self._timeout = timeout
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True).start()

        def _run(self, coro, timeout: float | None = None):
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            limit = self._timeout + 5 if timeout is None else timeout
            try:
                return fut.result(limit)
            except (TimeoutError, concurrent.futures.TimeoutError):
                # a timed-out BLE operation must not keep running (and
                # possibly complete later, e.g. a delayed write)
                fut.cancel()
                raise TimeoutError(
                    f"BLE operation timed out after {limit:.0f} s") from None

        def _stop_loop(self):
            self._loop.call_soon_threadsafe(self._loop.stop)

    class _WinConnectedNeewer(_LightMixin, _AsyncLoopMixin):
        """A handle that piggybacks (via WinRT) onto a NEEWER already
        connected to this PC (Windows).

        A unit held by the control app stops BLE advertising and therefore
        cannot be found by scanning, but the Windows BLE stack shares GATT
        connections across apps on the same PC, so it can be enumerated as
        an already-connected device and written to directly (the same idea
        as the CoreBluetooth piggyback on macOS). Raises RuntimeError if no
        connected NEEWER is found (the caller falls back to scanning).
        """

        def __init__(self, timeout: float = 15.0):
            self._dev = None
            self._start_loop(timeout)
            try:
                self._run(self._connect())
            except Exception:
                self._cleanup()
                raise

        def _cleanup(self) -> None:
            try:
                if self._dev is not None:
                    self._dev.close()
            except Exception:
                pass
            finally:
                self._dev = None
                self._stop_loop()

        async def _connect(self):
            import uuid as _uuid

            from winrt.windows.devices.bluetooth import (
                BluetoothConnectionStatus, BluetoothLEDevice)
            from winrt.windows.devices.bluetooth.genericattributeprofile import (
                GattCharacteristicProperties, GattCommunicationStatus,
                GattOpenStatus, GattSharingMode, GattWriteOption)
            from winrt.windows.devices.enumeration import DeviceInformation

            self._GattWriteOption = GattWriteOption
            self._GattCommunicationStatus = GattCommunicationStatus

            sel = BluetoothLEDevice.get_device_selector_from_connection_status(
                BluetoothConnectionStatus.CONNECTED)
            infos = await DeviceInformation.find_all_async_aqs_filter(sel)
            cands = []
            for i in range(infos.size):
                info = infos.get_at(i)
                if "NEEWER" in (info.name or "").upper():
                    cands.append({"id": info.id, "name": info.name, "obj": info})
            if not cands:
                raise RuntimeError("No connected NEEWER light found")
            info = pick_device(cands, _device_id())["obj"]
            self._name = info.name
            self._dev = await BluetoothLEDevice.from_id_async(info.id)
            if self._dev is None:
                raise RuntimeError("Cannot open the NEEWER device")
            svcs = await self._dev.get_gatt_services_for_uuid_async(
                _uuid.UUID(NEEWER_SVC))
            if svcs.status != GattCommunicationStatus.SUCCESS or svcs.services.size == 0:
                raise RuntimeError(
                    f"Cannot access the NEEWER service (status={svcs.status})")
            svc = svcs.services.get_at(0)
            # If the control app has opened the service exclusively, the
            # shared open fails with SHARING_VIOLATION and characteristics
            # cannot be enumerated either (observed in practice).
            open_st = await svc.open_async(GattSharingMode.SHARED_READ_AND_WRITE)
            if open_st == GattOpenStatus.SHARING_VIOLATION:
                raise RuntimeError(
                    "The control app is holding the light exclusively. "
                    "Quit the NEEWER control app on the PC and try again")
            if open_st not in (GattOpenStatus.SUCCESS,
                               GattOpenStatus.ALREADY_OPENED):
                raise RuntimeError(f"Cannot open the NEEWER service (status={open_st})")
            chs = await svc.get_characteristics_async()
            if chs.status != GattCommunicationStatus.SUCCESS:
                raise RuntimeError(
                    f"Cannot enumerate characteristics (status={chs.status})")
            W = GattCharacteristicProperties.WRITE
            WNR = GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
            items = []
            for i in range(chs.characteristics.size):
                ch = chs.characteristics.get_at(i)
                p = ch.characteristic_properties
                items.append((ch, bool(p & W), bool(p & WNR)))
            ch, with_resp = select_write_characteristic(items)
            self._write_ch = ch
            self._write_opt = (GattWriteOption.WRITE_WITH_RESPONSE if with_resp
                               else GattWriteOption.WRITE_WITHOUT_RESPONSE)

        async def _write(self, data: bytes):
            from winrt.windows.storage.streams import DataWriter
            w = DataWriter()
            # winrt 3.x's write_bytes requires a bytes-like object
            # (passing a list raises TypeError: a bytes-like object is required)
            w.write_bytes(bytes(data))
            status = await self._write_ch.write_value_with_option_async(
                w.detach_buffer(), self._write_opt)
            if status != self._GattCommunicationStatus.SUCCESS:
                raise RuntimeError(f"BLE write failed (status={status})")

        @property
        def name(self) -> str:
            return self._name

        def send(self, data: bytes) -> None:
            self._run(self._write(bytes(data)))

        def close(self) -> None:
            self._cleanup()

    class _BleakNeewer(_LightMixin, _AsyncLoopMixin):
        """A handle that finds and connects to a NEEWER directly via bleak's
        advertisement scan.

        For a unit not held by any app (i.e. advertising). Scans for
        SCAN_S seconds and refuses to guess if several lights answer.
        """

        def __init__(self, timeout: float = 15.0):
            from bleak import BleakClient, BleakScanner
            self._client = None
            self._start_loop(timeout)

            def _match(dev, adv):
                nm = (dev.name or adv.local_name or "").upper()
                uuids = [str(u).lower() for u in (adv.service_uuids or [])]
                return "NEEWER" in nm or NEEWER_SVC.lower() in uuids

            try:
                found = self._run(BleakScanner.discover(
                    timeout=min(timeout, SCAN_S), return_adv=True))
                cands = [{"id": dev.address,
                          "name": dev.name or adv.local_name or "NEEWER",
                          "obj": dev}
                         for dev, adv in found.values() if _match(dev, adv)]
                if not cands:
                    raise RuntimeError(
                        "No NEEWER light found (check that it is powered on; "
                        "it will not be found if another device such as a "
                        "phone is connected to it)")
                chosen = pick_device(cands, _device_id())
                self._name = chosen["name"]
                self._client = BleakClient(chosen["obj"])
                self._run(self._client.connect())
                self._write_ch, self._response = self._find_write_char()
            except Exception:
                self._cleanup()
                raise

        def _cleanup(self) -> None:
            try:
                if self._client is not None and self._client.is_connected:
                    self._run(self._client.disconnect())
            except Exception:
                pass
            finally:
                self._stop_loop()

        def _find_write_char(self):
            svc = self._client.services.get_service(NEEWER_SVC)
            if svc is None:  # some units use a different service UUID; fall back to a full scan
                chars = [ch for s in self._client.services for ch in s.characteristics]
            else:
                chars = list(svc.characteristics)
            return select_write_characteristic(
                [(ch, "write" in ch.properties,
                  "write-without-response" in ch.properties) for ch in chars])

        @property
        def name(self) -> str:
            return self._name

        def send(self, data: bytes) -> None:
            self._run(self._client.write_gatt_char(
                self._write_ch, bytes(data), response=self._response))

        def close(self) -> None:
            self._cleanup()

    def _win_neewer(timeout: float = 15.0):
        """Windows: try piggybacking on an already-connected device, then
        fall back to an advertisement scan (not when the piggyback found
        several lights: that must be resolved with NEEWER_DEVICE_ID)."""
        try:
            return _WinConnectedNeewer(timeout)
        except AmbiguousDeviceError:
            raise
        except Exception as e:
            print(f"Cannot piggyback on a connected device ({e}) -> falling "
                  f"back to scanning",
                  file=sys.stderr)
            return _BleakNeewer(timeout)


# Public name: pick the backend for the current platform
NeewerLight = _MacNeewer if IS_MAC else _win_neewer


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("on", "off", "cct", "hsi"):
        print(__doc__)
        return 1
    light = NeewerLight()
    try:
        if args[0] == "on":
            light.power(True)
            print(f"{light.name}: on")
        elif args[0] == "off":
            light.power(False)
            print(f"{light.name}: off")
        elif args[0] == "cct":
            brt, kelvin = int(args[1]), int(args[2])
            gm = int(args[3]) if len(args) > 3 else 50
            light.set_cct(brt, kelvin, gm)
            print(f"{light.name}: brightness {brt}% / {kelvin}K / GM{gm}")
        else:
            hue, sat, inten = int(args[1]), int(args[2]), int(args[3])
            light.set_hsi(hue, sat, inten)
            print(f"{light.name}: HSI {hue} deg / saturation {sat}% / brightness {inten}%")
    finally:
        light.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
