# Setup guide

From an empty control PC to the first automated dispense. Windows 11 is the
reference platform (the pipette and balance drivers use COM ports); the flow
executor, agent, GUI and voice input also run on macOS/Linux with `/dev/tty*`
device names.

## 1. Hardware

See [`bom.md`](bom.md) for the full list. The minimum for the ZIF-8 demonstration:

- 2 × robot arm (Dobot Magician), USB
- 2 × electric pipette (Sartorius Picus 2, 10 mL), USB (Bluetooth is also supported)
- 1 × electronic balance (Sartorius BCE822i) with RS-232C/USB serial
- 2 × hot-plate stirrer (IKA RET control-visc, or a cheaper IKA plate with the same
  NAMUR serial interface such as the IKA Plate (RCT digital); see `bom.md`) — optional, RS-232C/USB
- 1 × USB webcam
- 3D-printed fixtures from `cad/` (pipette holders on the arm tips, flask holders,
  balance splash guard)

Assembly: mount one pipette on each arm, place the receiving vial on the balance
between the arms, put each solution flask on its stirrer within reach of the
corresponding arm. **One arm + one pipette per solution**, so tips are never
exchanged.

Connect everything to the control PC and note the port of each device
(Windows: Device Manager → *Ports (COM & LPT)*; unplug/replug to identify).

## 2. Software

```bash
git clone https://github.com/YusukeHashimotoLab/saigen.git
cd saigen
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
pip install -r requirements.txt
```

Python 3.11–3.13. Optional extras:

- `ffmpeg` on PATH for voice input (`winget install ffmpeg` / `brew install ffmpeg`).
- A CUDA GPU speeds up whisper; CPU works with the int8 model.

## 3. Configuration (`config.yaml` and `.env`)

Two files, with different jobs: `config.yaml` holds the ports and the workspace
limits of your cell; `.env` holds API keys and per-run overrides. Both are
git-ignored.

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `config.yaml`:

| Section | Purpose |
|---|---|
| `robots` | `dobot_port` (and `picus2_address`, if that robot has a pipette) per `robot_id` |
| `shared_devices` | `scale_port` and `camera_index` of the balance and the photo camera |
| `workspace` | X/Y/Z and Joint-1 limits every move is checked against before it is commanded |

If `config.yaml` is absent the tracked `config.example.yaml` is used, and if that is
absent too the built-in defaults in `src/config.py` apply. Sensor-dashboard ports and
the video-recording camera index live in `src/monitoring/config.yaml` (section 8).

Edit `.env`:

| Variable | Purpose |
|---|---|
| `LLM_MODE` | `gemini` (default) or `openai_compatible` |
| `GEMINI_API_KEY` | required for `gemini`; free key from Google AI Studio |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | for any OpenAI-compatible endpoint (local inference server etc.) |
| `ROBOT1_DOBOT_PORT`, `ROBOT1_PICUS2_PORT`, `ROBOT2_*`, `ROBOT3_DOBOT_PORT` | optional per-run override of the `config.yaml` robot ports |
| `SCALE_PORT`, `CAMERA_INDEX` | optional per-run override of the shared devices |
| `SENSOR_SERVER_URL` | dashboard URL used to start/stop recording (optional) |

`.env` is git-ignored. No key is read from anywhere else. For device ports the
resolution order is: command-line flag > `.env` > `config.yaml` > built-in defaults.

## 4. Check the devices one by one

Each driver has a small self-test; run them with the arm clear of obstacles.

```bash
python -m src.devices.dobot.pydobot_controller          # lists serial ports, connects, reads position
python src/devices/picus2/test_picus2_minimal.py         # edit the port inside the file first
python -m src.devices.scale.scale_controller             # reads the balance
```

The Dobot Magician only knows its joint angles precisely after a **homing** run
(the firmware drives joint 1 to its end stop and re-zeroes the encoders). Do it
once after every power-on, one arm at a time, with the area around the arm clear:

```bash
python -m src.devices.dobot.home_dobot --robot 1 --mock   # rehearse the procedure, no hardware
python -m src.devices.dobot.home_dobot --robot 1          # real: asks for Enter before the arm moves
python -m src.devices.dobot.home_dobot --robot 2
```

The port comes from `config.yaml` (or `.env` / `--port`). `--target` chooses
where the arm returns after homing: `current` (default, the pose it started
from), `zero` (base angle 0°, same radius and height) or `config`
(`DobotConfig.HOME_SETTINGS`). The return pose is checked against the
`workspace` limits before anything is sent, and the tool refuses if it is
outside. The homing sweep itself is decided by the firmware and cannot be
validated, so keep the whole reach clear. Ctrl+C during the sweep is the same
emergency stop as in a flow: the queue is force-stopped and the arm halts.

For the arm, confirm the `workspace` section of `config.yaml` matches your fixtures
before running any flow: every `move_xyz` / `move_z` / `rotate` target is checked
against those limits **before** the robot is commanded, and a violation aborts the
run. The check also runs in `--mock` mode, so you can screen a flow for
out-of-range moves without hardware.

## 5. First run without hardware

```bash
python -m src.flow.run_flow examples/zif8/zif8_two_solution_mixing_speed5.json --validate-only
python -m src.flow.run_flow examples/zif8/zif8_two_solution_mixing_speed5.json --mock
```

`--validate-only` checks the JSON against the schema and expands loops.
`--mock` runs the whole flow against simulated robots and logs every action; the
workspace limits are enforced, and recording is always off. A mock run produces the
same `logs/<date>/<flow name>_<timestamp>/` folder as a real one, so you can inspect
`run.log`, `measurements.csv`, `summary.md` and `metadata.json` before touching
hardware.

Run the test suite too — it needs no hardware:

```bash
pytest tests/
```

## 6. First run with hardware

0. Home each arm once after power-on (`python -m src.devices.dobot.home_dobot
   --robot N`, see §4). Without it the joint angles the firmware reports can be
   off, and vertical `move_z` moves come out slanted.
1. Move each arm by hand (or with the driver self-test) to the pose you want as
   *home*: pipette tip above the flask, clear of everything. The runner captures the
   current pose as home at start-up; `go_home` returns there.
2. Check the reach: run the position-check presets (`src/agent/presets/wdc_2_dispense_robot1.json`
   etc.) — they move without aspirating.
3. Run the flow:

```bash
python -m src.flow.run_flow examples/zif8/zif8_two_solution_mixing_speed5.json \
    --robot1-dobot COM12 --robot1-picus2 COM4 \
    --robot2-dobot COM7  --robot2-picus2 COM5 \
    --scale-port COM9 --camera-index 1
```

Flags override `.env`, which overrides `config.yaml`. Only the robots referenced by
`robot_id` in the flow are initialised.

Press Ctrl+C to stop: this is an **emergency stop** — the Dobot command queue is
force-stopped so the move in progress halts immediately, the queue is discarded, the
conveyor motors and the IKA heater/stirrer are switched off, and no further motion
(not even going home) is commanded before the devices are disconnected.

Every run writes `logs/<YYYY-MM-DD>/<flow name>_<timestamp>/` containing `run.log`,
`measurements.csv` (one row per step, with the weights and image paths),
`summary.md`, `metadata.json`, `images/` and a copy of the flow JSON. Real runs also
record sensor CSV and overhead video by default; pass `--no-record` to skip that.

For the two-solution mixing experiment the parameters can be entered in a spreadsheet
instead of a JSON flow:

```bash
cp src/flow/csv_runner/control.example.csv src/flow/csv_runner/control.csv
python -m src.flow.csv_runner.run_csv --mock     # then without --mock
```

Same validation, same safety frame, same `logs/<date>/…` folder; the dispensed masses
are also written back to `results.csv` next to the sheet. See [`csv-runner.md`](csv-runner.md).

## 7. GUI and the AI agent

```bash
streamlit run src/gui/app.py
```

The browser opens at `http://localhost:8501`. In the sidebar set the ports and
toggle *Mock* on for a dry run. Then either

- pick a preset and press *Run*, or
- describe the experiment in natural language (typed, or spoken with the microphone
  button), let the agent produce the JSON flow, review and edit it in the step
  editor, and press *Run*.

The ports shown in the sidebar are read from `config.yaml` (see §3); edits there apply
to the current browser session only. Pressing *Run* first validates the flow against
`src/flow/schema.py` — a flow that fails is refused and the offending steps are listed,
so nothing is sent to the instruments. The run then goes through the same
`ExperimentSession` as the CLI and produces the same `logs/<date>/<flow>_<timestamp>/`
folder (`run.log`, `measurements.csv`, `dispense_accuracy.csv`, `metadata.json`,
`summary.md`, `images/`), whose path is shown under the progress bar. *Stop* cancels the
run, emergency-stops every robot and disconnects them. In Real mode, if the monitoring
dashboard is running, sensor recording is started and stopped around the run and its
`/api/is_safe` check gates each step. For the statistics of a dispensing run:

```bash
python -m src.gui.analyze_dispense_log logs/<date>/<flow>_<timestamp>
```

## 8. Monitoring (optional)

```bash
pip install -r src/monitoring/requirements.txt
cp src/monitoring/config.example.yaml src/monitoring/config.yaml
python src/monitoring/dashboard/launch_sensor_dashboard.py
```

opens the dashboard at `http://localhost:8000`, receives sensor data from Raspberry Pi
agents over TCP and records CSV/video into `experiment_data/`. Installing the Pi agent
and the network settings are described in `src/monitoring/README.md`.

## Troubleshooting

- **`GEMINI_API_KEY が設定されていません`** — the key is missing from `.env`, or the
  process was started from another directory (`.env` is loaded from the current
  working directory).
- **Pipette does not connect** — the Picus 2 must be switched on and in remote mode;
  the wrapper retries three times. On Bluetooth, pass the MAC address instead of a
  COM port and set `picus2_connection_type="bluetooth"`.
- **`WorkspaceViolationError`** — the target is outside the configured box; adjust the
  flow, or the `workspace` section of `config.yaml`. The message names the axis, the
  target value and the limit it broke.
- **Camera index** — enumeration order differs between machines; try 0, 1, 2.
