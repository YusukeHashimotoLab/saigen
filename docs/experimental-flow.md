# Experimental flow format

The AI agent outputs experimental procedures as a JSON "experimental flow" — an
intermediate representation connecting the natural-language experimental objective
with the control code actually executed. Constraining the LLM output to structured
JSON suppresses generation variability and enables mechanical validation before
execution.

The schema is defined with Pydantic in [`src/flow/schema.py`](../src/flow/schema.py)
and executed by [`src/flow/executor.py`](../src/flow/executor.py). Run
`python src/flow/schema.py` to print the machine-readable JSON Schema.

## Top level

```json
{
  "name": "human-readable name",
  "description": "what the flow does",
  "steps": [ { "action": "...", ... }, ... ]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | |
| `description` | string | no | default `""` |
| `steps` | array | yes | executed in order; each element is one of the actions below |

Every step has an `action` field. Robot actions take `robot_id` (1–3, default 1);
shared-device actions (balance, camera) do not. Unknown fields or out-of-range values
are rejected at validation time.

## Actions

### Robot arm (Dobot Magician) — `robot_id` required

| `action` | Parameters | Description |
|---|---|---|
| `move_xyz` | `x`, `y`, `z` (mm) | Absolute move in the arm's Cartesian frame |
| `move_z` | `distance` (mm) | Relative vertical move; positive = up |
| `move_radial` | `distance` (mm) | Relative move along the radius from the base; positive = outward |
| `rotate` | `angle` (deg) | Absolute base (joint 1) rotation |
| `rotate_relative` | `angle` (deg), `speed` (`"low"`/`"normal"`/`"high"`, default `"low"`) | Relative base rotation; positive = counter-clockwise seen from above (toward +Y) |
| `go_home` | — | Return to the home position captured at start-up |
| `move_slider` | `position` (0–1000 mm) | Linear rail (optional accessory) |
| `move_conveyer` | `index` (0/1), `speed` (0–200 mm/s), `duration` (0–300 s) | Conveyor belt (optional accessory) |

### Electric pipette (Sartorius Picus 2) — `robot_id` required

| `action` | Parameters | Description |
|---|---|---|
| `aspirate` | `volume` (0 < v ≤ 10 mL), `speed` (1–9, default 5) | Aspirate; the wrapper refuses to exceed 10 mL total |
| `dispense` | `volume` (0 < v ≤ 10 mL), `speed` (1–9, default 5) | Dispense; refuses to exceed the held volume |
| `blow_out` | `go_home` (bool, default true), `speed` (1–9, default 1), `delay_ms` (int, default 3000) | Expel residual liquid |

Speed 1 is slowest, 9 fastest (about 1–11 mL/s for a 10 mL tip, from the manufacturer's timing table).

### Shared devices — no `robot_id`

| `action` | Parameters | Description |
|---|---|---|
| `tare_scale` | `delay` (0.1–10 s, default 1.0) | Tare the electronic balance |
| `measure_weight` | `stabilization_count` (1–10, default 3) | Read the balance; median of N readings, logged in grams |
| `capture_and_save` | `file_path` (string, `""` = auto-generate) | Capture a webcam image |
| `capture_microscope` | `file_path` (string, `""` = auto-generate) | Capture an image with the USB digital microscope (UVC, e.g. Sanwa Supply 400-CAM106); saved into the run's `images/` folder with `_microscope` in the name. Opens only the microscope camera; `microscope_led` opens only its serial port; `microscope_focus` opens both |
| `microscope_led` | `on` (bool, default true), `level` (0–255, optional) | Switch the microscope's LED ring on or off and optionally set its brightness (factory level 12). Needs `microscope_port` in `config.yaml` (the UM22's CP210x serial port) |
| `microscope_focus` | `mode` (`auto` / `position` / `step`, default `auto`), `position` (0–65535, for `position`), `direction` (`in`/`out`) and `steps` (1–100, for `step`), `timeout` (1–300 s, default 60) | Focus the microscope: single-shot autofocus, move the lens motor to a recorded position, or step it. Waits until the motor stops and writes the lens position and whether it converged to the `focus_position` / `focus_converged` columns of `measurements.csv`. Autofocus needs a textured target and a non-saturating LED level; on a blank field it hunts until `timeout` and then falls back to manual mode. For reproducibility, record the position once and replay it with `mode: "position"`. Needs `microscope_port` |

### Utility and control flow

| `action` | Parameters | Description |
|---|---|---|
| `wait` | `seconds` (≥ 0) | Sleep |
| `loop_start` | `loop_id` (string), `count` (1–1000) | Begin a loop |
| `loop_end` | `loop_id` | End the loop with the same `loop_id` |

Loops are expanded into a flat list before execution (`expand_loops`). Nesting is not
supported; a `loop_start` without a matching `loop_end` (or vice versa) is an error.

## Validation rules

Applied before any device moves:

1. **Schema** — every step must match exactly one action model; types and ranges
   above are enforced by Pydantic.
2. **Loop structure** — `loop_id`s must pair up and must not nest.
3. **Pipette volume tracking** (run time, in the safety wrapper) — cumulative
   `aspirate` may not exceed 10 mL; `dispense` may not exceed the volume held.
4. **Workspace limits** (run time) — `move_xyz` / `move_z` / `rotate*` targets are
   checked against the XYZ box and joint-1 range in the `workspace` section of
   `config.yaml`; a violation raises before the command is sent, aborting the run.
   The same check runs in `--mock` mode, so a flow can be screened for out-of-range
   moves without hardware.

Conventions the agent's system prompt also enforces: Robot 3 (rail robot) carries
no pipette; each loop body and the whole flow end with `go_home`.

## Full example — ZIF-8 two-solution mixing

This is `examples/zif8/zif8_two_solution_mixing_speed5.json` (solution 2 dispensed at
pipette speed 5, nominal ≈1.45 s for 5 mL). The `speed1` and `speed9` variants differ
only in the `speed` of the second `dispense`.

The three files are the **JSON equivalents of the CSV-runner sequence** — the fixed
ten-step-per-robot sequence in [`docs/csv-runner.md`](csv-runner.md) that the paper's
ZIF-8 batches were actually run with — plus one closing `capture_and_save`, which the
CSV runs did not take. They are not the files that were executed for the paper; see
`examples/zif8/README.md`.

Running a flow leaves a record of the run in
`logs/<YYYY-MM-DD>/<flow name>_<timestamp>/`: `run.log`, `measurements.csv` (one row
per step, with the weights read, the nominal pipette duration of each aspirate/dispense
step from the Picus 2 timing table, and the photographs taken), `summary.md`,
`metadata.json`, `images/` and a copy of the flow JSON. See
[`src/flow/README.md`](../src/flow/README.md).

```json
{
  "name": "ZIF-8 two-solution mixing (solution 2 dispensed at pipette speed 5)",
  "description": "… (see the file)",
  "steps": [
    {"action": "move_z", "robot_id": 1, "distance": -0.1},
    {"action": "aspirate", "robot_id": 1, "volume": 5.0, "speed": 1},
    {"action": "move_z", "robot_id": 1, "distance": 0.1},
    {"action": "rotate_relative", "robot_id": 1, "angle": 90.0, "speed": "low"},
    {"action": "move_z", "robot_id": 1, "distance": -120.0},
    {"action": "tare_scale", "delay": 1.0},
    {"action": "dispense", "robot_id": 1, "volume": 5.0, "speed": 9},
    {"action": "measure_weight", "stabilization_count": 3},
    {"action": "move_z", "robot_id": 1, "distance": 120.0},
    {"action": "go_home", "robot_id": 1},

    {"action": "move_z", "robot_id": 2, "distance": -0.1},
    {"action": "aspirate", "robot_id": 2, "volume": 5.0, "speed": 1},
    {"action": "move_z", "robot_id": 2, "distance": 0.1},
    {"action": "rotate_relative", "robot_id": 2, "angle": -90.0, "speed": "low"},
    {"action": "move_z", "robot_id": 2, "distance": -120.0},
    {"action": "tare_scale", "delay": 1.0},
    {"action": "dispense", "robot_id": 2, "volume": 5.0, "speed": 5},
    {"action": "measure_weight", "stabilization_count": 3},
    {"action": "move_z", "robot_id": 2, "distance": 120.0},
    {"action": "go_home", "robot_id": 2},

    {"action": "capture_and_save", "file_path": ""}
  ]
}
```

Note the order inside each robot's block: the balance is tared only after the tip is
down in the vial, the mass is read before the tip rises again, and `go_home` — not a
second `rotate_relative` — brings the arm back.

Distances and angles are specific to the fixture geometry; see
`examples/zif8/README.md` for how to adapt them.

## Presets

Flows that have been checked on hardware are stored as *presets* in
`src/agent/presets/*.json`. A preset wraps a flow in `{"label", "description",
"template"}` and is offered in the GUI; the agent can also be asked to change only
the parameters of a preset (the step order is kept), which is the recommended way to
derive a new condition from a validated procedure.
