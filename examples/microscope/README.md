# USB digital microscope example

`product_closeup.json` switches the microscope's LED ring on at a
non-saturating level (`microscope_led`), runs a single-shot autofocus
(`microscope_focus`), takes one overhead photo with the webcam
(`capture_and_save`) and two close-ups with the USB digital microscope
(`capture_microscope`), switches the LED off and sends Robot 1 home. It is a device check as much as an experiment: it only
succeeds if the arm, the webcam, the microscope camera and the microscope's
control port can all be opened.

The paper's cell uses a Sanwa Supply **400-CAM106**, but any UVC microscope works
the same way: it is a plain USB Video Class camera, so no vendor driver is needed
and OpenCV opens it by its device index. The index is a per-PC value and lives in
`config.yaml`:

```yaml
shared_devices:
  camera_index: 1         # overhead webcam
  microscope_index: 2     # digital microscope (video)
  microscope_port: COM10  # digital microscope (LED control, CP210x serial port)
```

The 400-CAM106 is internally a Vitiny UM22: its single USB cable carries a UVC
camera and a Silicon Labs CP210x serial port for the control MCU. The LED
on/off and brightness go over that serial port (`microscope_led`); the
protocol is documented in `src/devices/microscope/README.md`. A microscope
without such a port can still be used for `capture_microscope`; just leave
`microscope_port` empty and drop the `microscope_led` steps.

To find the indices on Windows, run the driver's self-test; it prints the
DirectShow device names in index order (the 400-CAM106 appears as "UVC Video
Device") and saves one test frame:

```bash
python -m src.devices.microscope.microscope_controller 2
```

Then, as with every flow:

```bash
python -m src.flow.run_flow examples/microscope/product_closeup.json --validate-only
python -m src.flow.run_flow examples/microscope/product_closeup.json --mock
python -m src.flow.run_flow examples/microscope/product_closeup.json          # real; press Enter when asked
```

Images land in the run folder, `logs/<date>/<flow>_<time>/images/`, as
`stepNNN_<time>.jpg` for the webcam and `stepNNN_microscope_<time>.jpg` for the
microscope, and their paths appear in `measurements.csv` and `summary.md`.

A frame that is one flat colour means the lens cap is on, the tip is pressed
against the sample or the LED ring is off; the driver logs a warning but keeps
the image.

Autofocus only converges on a textured target; on a blank field the lens hunts
until `timeout` and the step then falls back to manual mode with
`focus_converged` false. The lens position after each focus step is written to
the `focus_position` column of `measurements.csv`. For a reproducible focus,
record that value once and replay it with
`{"action": "microscope_focus", "mode": "position", "position": <value>}`.
