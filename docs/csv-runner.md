# Spreadsheet (CSV) input — `src/flow/csv_runner`

The CSV runner is the shortest path from a spreadsheet to a run: the operator opens
one file in Excel, LibreOffice Calc or Numbers, types the volumes, heights, angles and
pipette speeds of a **two-solution mixing** experiment, saves, and runs one command.
No JSON is written by hand and no GUI is needed.

**This is the path used for the paper's ZIF-8 experiments.** The nine reported
batches were executed with the lab's version of this runner (internal
`excel_runner/run_csv.py`, commit `4584333`, in force from 2026-07-02), not with
hand-written or LLM-generated JSON. The runner published here reproduces that
sequence step for step.

The experiment itself is fixed. The runner always performs the same ten steps per
robot (plus two optional ones, 4a and 7a, that are off by default), Robot 1 first and
then Robot 2, and only substitutes the numbers from the CSV:

    for each robot (1, then 2), if its volume is not 0:
        1  move_z(-aspirate_z_descent_mm)     lower onto its source vessel
        2  aspirate                           (robot*_volume_mL, aspirate_speed)
        3  move_z(+aspirate_z_descent_mm)     rise
        4  rotate_relative(angle, "low")      swing over the vial on the balance
        4a move_radial(robot*_dispense_radial_mm)   only if the offset is not 0
        5  move_z(-dispense_z_descent_mm)     lower into the vial
        6  tare_scale(delay=1.0)              tare with the tip already lowered
        7  dispense                           (robot*_volume_mL, robot*_dispense_speed)
        7a blow_out(speed = dispense speed, 3 s)    only if blow_out_after_dispense
        8  measure_weight                     read the mass BEFORE the tip rises
        9  move_z(+dispense_z_descent_mm)     rise
       10  go_home                            the home move also undoes the rotation

    once, only if capture_photo is TRUE:
        capture_and_save                      photograph the vial

Three details of that order are easy to get wrong and are worth stating explicitly:
the balance is tared **after** the tip is already down in the vial, not before the arm
moves; the mass is read **before** the tip rises, so no vibration from the Z move
enters the reading; and there is **no `rotate_relative` back** — `go_home` returns the
arm. The photograph, the radial offset and the blow-out are not part of the historical
sequence: `capture_photo`, `robot*_dispense_radial_mm` and `blow_out_after_dispense`
are off by default, so the default run is exactly the sequence the paper's batches
used, and a sheet that turns them on reproduces the lab's current procedure (see
"Dispense-position offset and blow-out" below).

`examples/zif8/zif8_two_solution_mixing_speed*.json` are the JSON equivalents of this
sequence (plus one closing photograph); see
[`examples/zif8/README.md`](../examples/zif8/README.md).

Anything else — stirring, imaging in the middle of a run, loops, three robots, a
different order — is a JSON flow; see
[`docs/experimental-flow.md`](experimental-flow.md).

## The CSV

`parameter,value,note` — one row per parameter, and the `note` column is free text for
the operator. Row order does not matter. A row whose `parameter` cell is empty or
starts with `#` is a comment and is ignored, so a lab can add its own notes that way.
Every other row must name one of the parameters below **exactly once**: an unknown
name (typically a typo such as `robot1_volume_ml`, which would otherwise leave the
real row at its default) and a parameter given twice are both errors, reported with
their line number in the file (the header is line 1). The optional rows listed below
may still be left out. Copy the published example to your own working file:

```bash
cp src/flow/csv_runner/control.example.csv src/flow/csv_runner/control.csv
```

`control.csv` is gitignored: it holds one lab's numbers, not a published setting.

| `parameter` | Type | Range | Meaning |
|---|---|---|---|
| `robot1_volume_mL` | float | 0, or 0.5–10 | Robot 1 aspirate/dispense volume. **0 skips Robot 1 entirely**; any other value must be at least the pipette wrapper's minimum of 0.5 mL |
| `robot2_volume_mL` | float | 0, or 0.5–10 | Robot 2 aspirate/dispense volume. **0 skips Robot 2 entirely**; otherwise at least 0.5 mL |
| `aspirate_z_descent_mm` | float | 0.1–200 | Z descent onto the source vial before aspirating |
| `dispense_z_descent_mm` | float | 0.1–200 | Z descent onto the vial on the balance before dispensing |
| `aspirate_speed` | int | 1–9 | Pipette speed used for aspiration by both robots. Optional row; **1** if absent |
| `robot1_dispense_speed` | int | 1–9 | Robot 1 pipette speed for dispensing (1 slowest, 9 fastest) |
| `robot2_dispense_speed` | int | 1–9 | Robot 2 pipette speed for dispensing |
| `robot1_angle_deg` | float | −360–360 | Robot 1 base rotation from the source vial to the balance |
| `robot2_angle_deg` | float | −360–360 | Robot 2 base rotation from the source vial to the balance |
| `robot1_dispense_radial_mm` | float | −100–100 | Radial offset of Robot 1's dispense position, applied after the rotation and before the Z descent; negative = towards the base. Optional row; **0** if absent |
| `robot2_dispense_radial_mm` | float | −100–100 | Same for Robot 2. Optional row; **0** if absent |
| `blow_out_after_dispense` | bool | TRUE/FALSE | Blow out the tip after dispensing, before the mass is read (same speed as the dispense, 3 s). Optional row; **FALSE** if absent |
| `capture_photo` | bool | TRUE/FALSE | Photograph the vial once at the end of the run. Optional row; **FALSE** if absent |

The ranges above mirror the constraints in `src/flow/schema.py`. Every value is
converted and range-checked **before any device is opened**, and all problems in the
sheet are reported at once, so a typo can never reach the hardware.

Being inside these ranges is not the same as being safe in *your* cell: the generated
steps go through the same **workspace preflight** as a JSON flow (`run_flow.run_preflight`,
before any device is opened, with `--validate-only` too; a violation exits with code 3).
The fixed sequence consists only of relative moves, which the preflight lists as
"unverified until run", so in practice they are checked move by move, against the `workspace` limits in `config.yaml`
by the same `WorkspaceValidator` that guards the JSON flows (in `--mock` too). Set
those limits for your own bench before a real run — see
[`docs/setup.md`](setup.md).

### Aspiration speed

In the lab version there was no `aspirate_speed` row at all: aspiration used the code
constant `ASPIRATE_SPEED = 1`, and that is the speed at which every ZIF-8 batch in the
paper aspirated. (The note column of the lab's sheet read "aspiration is always 5";
that note was stale and never affected the runs — the code, not the note, set the
speed.) Here it is an ordinary CSV parameter, `aspirate_speed` (1–9), applied to both
robots and written into the generated `aspirate` steps and into `results.csv` with
everything else. **Its default is 1**, so a sheet that leaves the row out reproduces
the historical behaviour exactly; the CSV rows `robot*_dispense_speed` apply only to
dispensing, which is the variable the published runs compare (1 / 5 / 9).

### Dispense-position offset and blow-out

Two optional rows reproduce the additions the lab runner gained after the paper's
batches (lab commit fc68096). `robotN_dispense_radial_mm` inserts a `move_radial`
step right after the rotation, before the Z descent: on a fixture where the vial on
the balance does not sit exactly on the rotation arc, a negative value pulls the tip
towards the base and a positive one pushes it outwards. `blow_out_after_dispense`
inserts a `blow_out` step (piston to its end and back, at the dispense speed, 3 s)
between the dispense and the weighing, so that liquid left in the tip is counted in
the measured mass. Both are off by default, and with both off the generated steps are
identical to the historical sequence. The extra moves go through the same schema and
workspace checks as every other step.

### Validation, an addition over the lab version

Unlike the lab version, which called the device layer directly, the published runner
first assembles the steps as data and **validates them against the flow schema
(`ExperimentWorkflow` in `src/flow/schema.py`) and against the workspace limits in
`config.yaml`** before anything moves — the same two checks that guard a JSON flow, in
`--mock` as well as in a real run. A value that is inside the CSV's own parameter
range but outside your cell (a 200° rotation, say) therefore fails before the arm is
commanded rather than during the run.

## Running

```bash
# dry run: no hardware, every step logged, recording always off
python -m src.flow.csv_runner.run_csv --mock

# check the sheet and the generated step list without running anything
python -m src.flow.csv_runner.run_csv --validate-only

# real run, using src/flow/csv_runner/control.csv and the ports in config.yaml
python -m src.flow.csv_runner.run_csv

# a different sheet, e.g. one per condition
python -m src.flow.csv_runner.run_csv --csv sheets/zif8_speed9.csv

# real run without sensor-CSV/video recording, dispensing ethanol
python -m src.flow.csv_runner.run_csv --no-record --liquid-density 0.789
```

Without `--csv`, the runner uses `src/flow/csv_runner/control.csv` if it exists and
falls back to the published `control.example.csv` if it does not — so `--mock` works in
a fresh clone.

| Option | Meaning |
|---|---|
| `--csv PATH` | input sheet (default: `control.csv`, else `control.example.csv`) |
| `--mock` | run against simulated devices; recording is always off |
| `--validate-only` | read the sheet, build and schema-check the steps and run the workspace preflight, then stop (no device is opened) |
| `--record` / `--no-record` | sensor-CSV and video recording; on by default for real runs |
| `--camera-index N` | camera used when `capture_photo` is TRUE (default: `CAMERA_INDEX` / `config.yaml`) |
| `--liquid-density X` | density in g/mL used to turn dispensed volume into expected mass (default 1.0) |
| `--robot1-dobot`, `--robot1-picus2`, … `--scale-port` | per-run port overrides, exactly as in `run_flow.py` |
| `-v` | debug logging |

Ports come from the same place as every other runner: command-line flag → environment
variable (`.env`) → `config.yaml` → built-in defaults.

Ctrl+C triggers the same **emergency stop** as a JSON run (queue force-stopped, no
further motion commanded, devices disconnected, run recorded as `aborted`).

## Where the results go

A CSV run leaves behind exactly what a JSON run leaves behind, in the same place:

    logs/<YYYY-MM-DD>/two-solution mixing (CSV input)_<timestamp>/
        run.log                  full log of the run
        measurements.csv         one row per step (weights included)
        summary.md               human-readable report
        metadata.json            status, execution mode (mock/real), robots used,
                                 counts, sensor-recording ownership
        dispense_accuracy.csv    expected vs. measured mass per dispense
        control.csv              a copy of the sheet the run was started from
        generated_flow.json      the exact steps that were executed
        images/                  (empty unless capture_photo is TRUE)

`measurements.csv` has a `mode` column and `summary.md` states the execution mode,
so a mock run can never be mistaken for measured data. Two runs started in the same
second get separate folders (`…_<timestamp>_2`, `_3`, …); a run never writes into an
existing folder. With recording on, the runner stops the dashboard's sensor recording
at the end only if this run started it (`/api/start` answered 200); if a recording
was already running (409) it is left alone, and `metadata.json` records which case
applied (`sensor_recording.started_by_this_run`).

`generated_flow.json` is a normal flow file: it can be re-run with
`python -m src.flow.run_flow <that file>`, which makes a spreadsheet run reproducible
by someone who never sees the spreadsheet.

For convenience, the runner also writes `results.csv` **next to the input sheet**, so
the operator can open the parameters and the two dispensed masses in the same
spreadsheet program:

```
key,value
last_run_at,2025-07-12 10:04:31
robot1_volume_mL,5.0
...
robot1_weight_g,4.98
robot2_weight_g,4.97
log_dir,/…/logs/2025-07-12/two-solution mixing (CSV input)_20250712_100418
```

If `results.csv` is open in the spreadsheet program (Windows locks it), a timestamped
`results_<date>_<time>.csv` is written instead. Both are gitignored.

## How this relates to the JSON flows and the GUI

There are three ways to describe an experiment in this repository; all three converge
on the same validated step list, the same executor, the same safety frame and the same
log folder:

| Path | Input | Best for |
|---|---|---|
| **CSV** (`src/flow/csv_runner`) | a spreadsheet of numbers | repeating *this one* experiment with different values — no software skills needed |
| **JSON** (`src/flow/run_flow`) | a full flow file | any sequence: loops, imaging, stirring, more robots |
| **GUI** (`src/gui`) | browser: cards, presets, natural language, voice | building or editing a flow interactively |

The CSV runner does not re-implement any of this. It builds a Python list of steps,
validates it with `ExperimentWorkflow` (`src/flow/schema.py`), and executes it with
`execute_step()` inside an `ExperimentSession`, recording through `ExperimentLogger`
and `DispenseAccuracyLogger` — the same objects `run_flow.py` uses. A change to the
schema, the workspace validator or the logging therefore reaches the spreadsheet path
automatically.

## Relation to the paper

The paper states that experimental parameters "can be set by direct input via
spreadsheet software". This runner is that path, and it is the path the reported
ZIF-8 batches actually took: the operator's only input was a `control.csv` opened in a
spreadsheet program, and one command turned those cells into a run of the fixed
two-solution mixing sequence above.

`control.example.csv` carries the values of the lab's committed control sheet at
commit `4584333` — 5 mL per solution, 0.1 mm aspiration descent, 120 mm dispensing
descent, ±90° rotations, Robot 1 dispensing at speed 9, Robot 2 at speed 1, aspiration
at speed 1. Those are the sheet's values at that commit, **not necessarily the values
of every batch**: the dispensing speed of Robot 2 was varied over 1 / 5 / 9 across the
nine batches. The per-batch run records are not published yet;
until they are, treat the example sheet as the shape of the
input, not as a run record.
