# Guidance for AI coding agents working in this repository

This file is read by Claude Code (`CLAUDE.md`) and by other agents (`AGENTS.md`,
an identical copy). It tells an agent what SAIGEN is, how to verify changes, and
which parts must not be changed on the agent's own judgement. The human-facing
introduction is `README.md`; read it first.

## What this repository is

SAIGEN (Standardized Automation Infrastructure for Generalizable Experiment
reproductioN) is the control software of a small automated materials-synthesis
cell: Dobot Magician robot arms with Picus 2 electronic pipettes, an electronic
balance, IKA hot-plate stirrers, webcams and a Raspberry Pi sensor logger.
Experiments are described as JSON "flows" (or as a parameter spreadsheet) and
executed step by step through a safety wrapper that checks every robot move
against configured workspace limits before the hardware is commanded.

The typical reason a user hands this repository to an agent is to adapt it to
their own hardware: different ports, a different number of robots, no balance,
a different vial layout, a new experiment. Most of that adaptation belongs in
configuration and flow files, not in the driver code.

## Where things live

| Path | Role |
|---|---|
| `config.example.yaml` -> `config.yaml` | Serial ports per robot, shared devices, robot workspace limits. `config.yaml` is gitignored. |
| `.env.example` -> `.env` | API keys for flow generation, optional per-run port overrides, monitoring settings. Never committed. |
| `src/flow/` | JSON flow schema, validator, executor and the CLI runner `run_flow.py`. |
| `src/flow/csv_runner/` | Spreadsheet-driven runner for the two-solution mixing experiment (`run_csv.py`). |
| `src/devices/safety/` | `LabRobot` (real), `MockLabRobot` (no hardware), shared devices and the workspace validators. **All flows run through this layer.** |
| `src/devices/{dobot,picus2,scale,ika,webcam}/` | Per-instrument drivers. |
| `src/agent/` | LLM-based generation of flows from natural language, plus preset flows. |
| `src/gui/` | Streamlit GUI. `src/voice/` voice input. |
| `src/monitoring/` | Sensor dashboard (FastAPI) and Raspberry Pi agent; own `config.example.yaml`. |
| `src/imaging/` | Appearance-imaging system (light + camera), separate README. |
| `detection/` | YOLOv8 object detection. **AGPL-3.0**, isolated on purpose; never import it from `src/`. |
| `examples/zif8/` | Example flows of the paper's ZIF-8 demonstration. |
| `docs/` | Bill of materials, setup guide, flow format reference, spreadsheet guide, guide to adding an instrument. |
| `tests/`, `src/monitoring/tests/` | Unit tests; none need hardware. |

## How to verify a change (no hardware needed)

```bash
pip install -r requirements.txt            # or the lighter set in .github/workflows/ci.yml
python -m pytest tests/ src/monitoring/tests/ -q
python -m src.flow.run_flow examples/zif8/zif8_two_solution_mixing_speed5.json --validate-only
python -m src.flow.run_flow examples/zif8/zif8_two_solution_mixing_speed5.json --mock
python -m src.flow.csv_runner.run_csv --mock
```

`--validate-only` checks a flow against the schema and the workspace limits
without opening any device. `--mock` executes it end to end against simulated
devices, with the same validation. Run both after any change to a flow, the
schema, the executor or `config.yaml`. The CI workflow in
`.github/workflows/ci.yml` runs the same steps and must stay green.

## Rules

1. **Never start a real run on your own.** Anything without `--mock` (or
   without *Mock* in the GUI) moves a robot arm and dispenses liquid. Run
   `--validate-only`, then `--mock`, and stop there. A real run happens only
   when the human explicitly asks for it in the current conversation, after the
   mock run succeeded, and it is the human who presses Enter. Ctrl+C is the
   emergency stop: the arm halts where it is.
2. **Do not weaken the safety layer.** Do not edit `src/devices/safety/`, the
   workspace validators, or the `workspace` limits in `config.yaml` in order to
   make a failing move pass. A move rejected by the validator is the safety
   system doing its job; report it to the human with the offending coordinates
   and let them decide, after measuring their cell. Only tighten limits on your
   own.
3. **Adapt through configuration and flows, not drivers.** Ports, robot count,
   presence of a pipette or balance, vial positions, volumes, speeds and
   step order are all expressible in `config.yaml`, `.env`, JSON flows and the
   CSV sheet. Change those first. Edit the drivers in `src/devices/` only for
   a genuine defect or a new instrument, and add a test when you do. Keeping
   the shared code unchanged is what makes runs comparable between labs.
4. **Keep secrets and lab-specific values out of the tree.** API keys go in
   `.env`; ports and limits in `config.yaml`; both are gitignored. Never write
   real keys, IP addresses of lab machines, or Bluetooth addresses into tracked
   files, examples or documentation.
5. **Respect the licence boundary.** `detection/` depends on AGPL-3.0
   Ultralytics. Nothing under `src/`, `examples/` or `tests/` may import it.
6. **Do not invent measurements.** If a user asks for data the repository does
   not contain (the paper's raw batch logs, camera images, sensor CSVs), say so.
   `examples/zif8/` contains flows and CSV templates, not the published
   measurements, except where a README says otherwise.
7. **Write tests for what you change** and run the full suite before reporting
   done. Tests must keep working without hardware; use `MockLabRobot` and
   `MockSharedDevices` from `src/devices/safety/`.

## Typical tasks and where to start

- *"Set this up for my hardware"*: copy `config.example.yaml` to `config.yaml`
  and `.env.example` to `.env`; ask the user for the ports (on Windows they are
  in Device Manager, on Linux/macOS `ls /dev/tty*`); then `--validate-only`
  and `--mock` an example flow. Only the robots and devices a flow's steps
  actually reference are opened, so entries for hardware the user does not
  have can stay in `config.yaml` (it is deep-merged over built-in defaults, so
  deleting an entry does not remove it).
- *"Write a flow for experiment X"*: read `docs/experimental-flow.md`, start
  from the nearest file in `examples/zif8/`, validate, mock-run, then hand the
  file to the user for a real run. `src/agent/` can also draft flows from
  natural language if an API key is configured.
- *"The robot hit something / a move was rejected"*: see rule 2. Collect the
  coordinates from `logs/<date>/<flow>_<time>/run.log` and report.
- *"Add a new instrument"*: follow `docs/adding-an-instrument.md` step by
  step: a driver package under `src/devices/`, a mock counterpart, registration
  with `SharedDevices` / `MockSharedDevices`, the flow schema and executor, the
  run-log column, configuration keys, tests, documentation and, if the step
  should be available from the GUI, the block palette in `src/gui/app.py`.
