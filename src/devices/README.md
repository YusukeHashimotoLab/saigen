# `src/devices` — instrument drivers and safety wrapper

Two layers:

- **Drivers** (`dobot/`, `picus2/`, `ika/`, `scale/`, `webcam/`, `microscope/`): thin, device-specific
  classes that speak the instrument's protocol. They know nothing about experiments.
- **Safety wrapper** (`safety/`): the only layer the flow executor and the AI agent
  ever call. It adds workspace limits, pipette volume tracking, retries, settle
  times and logging on top of the drivers.

The AI agent never generates driver-level code; it only chooses *what* to do, and the
wrapper decides *how*.

## Drivers

| Directory | Instrument | Interface | Library | Main class |
|---|---|---|---|---|
| `dobot/` | Dobot Magician robot arm (+ optional rail / conveyor) | USB serial | `pydobot` (+ small patch in `pydobot_patch.py`) | `PyDobotController` |
| `picus2/` | Sartorius Picus 2 electric pipette (10 mL) | USB serial or Bluetooth LE | `pyserial` / `bleak` | `Picus2Controller` |
| `ika/` | IKA RET control-visc hot-plate stirrer | RS-232C | `pyserial` | `IKAController` |
| `scale/` | Sartorius BCE822i (`BCE8221.py`, used in the paper) and A&D EK-610i (`Ek610i.py`, `scale_controller.py`) balances | RS-232C | `pyserial` | `SerialBalance`, `ScaleController` |
| `webcam/` | USB webcam for process photos | UVC | `opencv-python` | `WebcamController` |
| `microscope/` | USB digital microscope for close-up photos (Sanwa Supply 400-CAM106 = Vitiny UM22 in the paper's cell) | UVC (image) + USB serial via CP210x (LED, focus motor, status) | `opencv-python`, `pyserial` | `MicroscopeController` (subclass of `WebcamController`), `UM22SerialController` |

Each directory has its own README (in Japanese) with the command set and a usage
example. Device addresses (COM port, Bluetooth MAC) are always passed in by the
caller; there are no hard-coded addresses.

## Safety wrapper (`safety/`)

| File | Class | Role |
|---|---|---|
| `lab_robot.py` | `LabRobot` | One robot = one arm + optionally one pipette (+ stirrer). Async API used by the executor: `move_xyz`, `move_z`, `move_radial`, `rotate`, `rotate_relative`, `go_home`, `aspirate`, `dispense`, `blow_out`, `move_slider`, `move_conveyer`. Tracks the current pose and the volume held in the tip. |
| `shared_devices.py` | `SharedDevices` | Balance, camera and microscope, shared by all robots: `tare_scale`, `measure_weight`, `capture_and_save`, `capture_microscope`, `set_microscope_led`, `focus_microscope`. |
| `mock_robot.py` | `MockLabRobot`, `MockSharedDevices` | Same interface as `LabRobot` / `SharedDevices` without hardware; used by `--mock` and the GUI's Mock mode. The mock enforces the same workspace limits and returns realistic weights, so mock runs produce the same records as real ones. |
| `validators/` | `WorkspaceValidator` | XYZ box and joint-1 angle limits, checked before every move. The limits come from the `workspace` section of `config.yaml` in the repository root (`default_workspace_validator()`); edit that file to match your fixtures. |

Construction:

```python
from src.devices.safety.lab_robot import LabRobot
from src.devices.safety.shared_devices import SharedDevices

async with LabRobot(use_dobot=True, use_picus2=True,
                    dobot_port="COM12", picus2_address="COM4",
                    picus2_connection_type="usb") as robot:
    robot.set_current_position_as_home()
    await robot.move_z(-50)
    await robot.aspirate(volume=5.0, speed=5)
    await robot.move_z(50)
    await robot.go_home()

async with SharedDevices(use_scale=True, scale_port="COM9") as shared:
    await shared.tare_scale()
    grams = await shared.measure_weight(stabilization_count=3)
```

Safety behaviour built into the wrapper:

- `aspirate` refuses to exceed 10 mL in the tip; `dispense` refuses to exceed the
  held volume.
- Every target pose is validated against the workspace limits from `config.yaml`;
  a violation raises `WorkspaceViolationError` before the command is sent.
- `initialize()` raises if any device fails to come up (and disconnects whatever did
  connect) rather than returning a status that a caller could ignore and then drive a
  disconnected arm.
- `emergency_stop()` force-stops the Dobot command queue (the move in progress halts
  immediately, the queue is discarded, the conveyor motors stop) and switches the IKA
  heater/stirrer off. `run_flow.py` calls it on Ctrl+C, and commands no further
  motion afterwards.
- Configurable settle times after moves, pipette actions and balance readings
  (`wait_after_*` keyword arguments).
- Pipette connection is retried three times with back-off.
- `cleanup()` (also on `async with` exit or on error) disconnects every device.

## Not included

The vendor DLL-based Dobot driver and the powder-dispenser driver used in other
experiments are not part of this repository; only the `pydobot`-based arm driver is
shipped. `LabRobot(use_powder_dispenser=True)` therefore raises `ImportError`.
