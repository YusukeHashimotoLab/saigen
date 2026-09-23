# Monitoring

IoT environmental sensor logging and process video recording for the automated
synthesis platform. A Raspberry Pi Zero WH + Waveshare Environment Sensor HAT
pushes sensor readings to the control PC, where a small FastAPI service
records them (plus an overhead-camera AprilTag pose track) to CSV; a separate
OpenCV helper records the overhead-camera video itself.

## Overview

```
 ┌────────────────────┐        TCP :50001            ┌───────────────────────────────────┐
 │ Raspberry Pi        │  JSON Lines, ~10 Hz          │ Control PC                          │
 │ pi/pi_sensor_agent  │ ────────────────────────────>│ dashboard/launch_sensor_dashboard   │
 │  (I2C sensor reads)  │                              │  FastAPI :8000  +  TCP server :50001│
 └────────────────────┘                              │                                      │
                                                       │  latest_sensor_data ──┐              │
                                                       │                       ▼              │
                                                       │              recording_loop (100 ms) │
                                                       │                       │              │
                                                       │                       ▼              │
                                                       │              CSV (sensor + tags)     │
                                                       └───────────────────────────────────┘
                    browser (dashboard UI)                       ▲        ▲
                        │  camera frames                          │        │
                        └── POST /api/detect_tags ─────────────────┘        │
                        └── POST /api/upload_video (webm) ───────────────────┘

 src/flow/run_flow.py, src/gui/app.py or any external process
        │
        └── POST /api/start , POST /api/end  ────────► start/stop a recording session

 camera/video_recorder.py  (on the control PC, independent of the dashboard)
        └── OpenCV VideoCapture(camera_index) ──► .mp4 (overhead process video)
```

Two recording paths run independently:

1. **Sensor + AprilTag CSV logging** — `dashboard/launch_sensor_dashboard.py`
   receives sensor pushes over TCP, serves a browser dashboard over HTTP, and
   (while a recording session is active) writes one CSV row per Pi every
   100 ms plus AprilTag pose rows whenever the browser posts a camera frame
   to `/api/detect_tags`.
2. **Overhead video recording** — `camera/video_recorder.py` opens an OpenCV
   `VideoCapture` on the process camera and writes an `.mp4` on its own
   thread. It has no dependency on the dashboard and can be started/stopped
   independently (e.g. from `src/flow/run_flow.py`).

## Measured quantities

| Quantity | Sensor | I2C address | CSV column(s) |
|---|---|---|---|
| Temperature | BME280 | `0x76` | `temperature` (°C) |
| Humidity | BME280 | `0x76` | `humidity` (%RH) |
| Illuminance | TSL25911 | `0x29` | `lux` |
| UV | LTR390 | `0x53` | `uvi` (UV index) |
| VOC | SGP40 | `0x59` | `voc_raw` (raw signal, not the VOC index) |
| 3-axis acceleration | ICM20948 | `0x68` | `acc_x`, `acc_y`, `acc_z` (g) |
| 3-axis angular velocity | ICM20948 | `0x68` | `gyro_x`, `gyro_y`, `gyro_z` (deg/s) |

**The magnetometer (AK09916, embedded in the ICM20948 package) is not read in
this version.** Only the ICM20948's accelerometer and gyroscope are used.

**UV column semantics changed on 2026-09-08.** Recordings made before that date
were taken with the LTR390 in ALS mode, in which the UVS data registers never
update, so their `uvi` column is `0` throughout. The Pi agent now initialises
the sensor in UVS mode (20-bit, gain 3, 1000 ms measurement rate) and `uvi`
holds a UV index as a float. The column name is unchanged so older CSV files
remain parseable. Because a 20-bit conversion takes 400 ms, the value updates
about once a second even though the agent samples every 100 ms.

**Humidity recorded before 2026-09-23 is wrong by a factor of 1024.** The Pi
agent's BME280 compensation shifted the Q22.10 result right by 22 bits (which
already gives whole %RH) and then divided by 1024 again, so the `humidity`
column of older recordings holds values between 0 and about 0.098 instead of
0-100 %RH. Multiplying an old value by 1024 recovers the humidity, but only to
1 %RH: the 22-bit shift had already dropped the fractional part, so every
recovered value is a whole number (rounded down). Recordings made with the
fixed agent carry the full resolution of 1/1024 %RH.

## Raspberry Pi setup

1. Flash **Raspberry Pi OS Lite (Bookworm, 32-bit)** to the SD card with
   [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Use the
   Imager's own settings (gear icon / "Edit Settings") to set the hostname,
   create a user, and configure Wi-Fi — **never commit these credentials to
   the repository.**
2. Boot the Pi, SSH in, and enable I2C:
   ```bash
   sudo raspi-config
   # Interface Options -> I2C -> Enable
   ```
3. Install the I2C tools and Python bus library:
   ```bash
   sudo apt update
   sudo apt install -y python3-smbus i2c-tools
   ```
4. Power off the Pi and mount the Waveshare Environment Sensor HAT on the
   40-pin GPIO header.
5. Boot again and verify the sensors are visible on the bus:
   ```bash
   i2cdetect -y 1
   ```
   Expected addresses: `0x29` (TSL25911), `0x53` (LTR390), `0x59` (SGP40),
   `0x68` (ICM20948), `0x76` (BME280).
6. Copy the agent and service files from this repository to the Pi (see
   [Installing the agent](#installing-the-agent) below), or do it manually:
   ```bash
   scp src/monitoring/pi/pi_sensor_agent.py pi@<pi-host>:~/pi_sensor_agent.py
   ```
7. Create `/etc/sensor-agent.env` on the Pi from
   [`pi/sensor-agent.env.example`](pi/sensor-agent.env.example), filling in
   your control PC's address:
   ```bash
   ssh pi@<pi-host> "sudo tee /etc/sensor-agent.env >/dev/null" < src/monitoring/pi/sensor-agent.env.example
   ssh pi@<pi-host> "sudo chmod 600 /etc/sensor-agent.env"
   # then edit SENSOR_AGENT_PC_HOST on the Pi to your PC's real address
   ```
8. Install and enable the systemd service:
   ```bash
   ssh pi@<pi-host> "sudo cp ~/sensor-agent.service /etc/systemd/system/ && \
       sudo systemctl daemon-reload && sudo systemctl enable --now sensor-agent"
   ```
9. Check it is running and reconnecting correctly:
   ```bash
   ssh pi@<pi-host> "journalctl -u sensor-agent -f"
   ```

### Installing the agent

[`pi/install_pi_agent.ps1`](pi/install_pi_agent.ps1) automates steps 6-8 from
a Windows control PC:

```powershell
.\src\monitoring\pi\install_pi_agent.ps1 -User pi -HostName raspberrypi.local -PcHost 192.0.2.10
```

This copies `pi_sensor_agent.py`, renders `sensor-agent.service` with `%i`
substituted for the given user, renders `/etc/sensor-agent.env` with the
given `-PcHost`/`-PcPort`, and enables the service over SSH. Run it once per
Pi; after that, the agent starts automatically on every boot and reconnects
to the PC whenever the connection drops. Re-run the script (or just update
`/etc/sensor-agent.env` and `systemctl restart sensor-agent`) if the control
PC's address changes.

## Network

- The Pi and the control PC must be reachable on the same LAN. Any LAN
  works — the reference setup used a Windows mobile hotspot hosted by the
  control PC, mainly because it makes the PC's gateway address predictable
  and keeps mDNS (`*.local`) hostname resolution reliable without depending
  on lab infrastructure, but a normal router or switch works identically.
- The Pi's target address is configured on the Pi side, via
  `SENSOR_AGENT_PC_HOST` in `/etc/sensor-agent.env` — **not** hardcoded in
  `pi_sensor_agent.py`. There is no default; the agent exits with an error if
  the variable is unset and no `--host` argument is given.
- The dashboard's web server listens on `127.0.0.1` (loopback only) by
  default — a remote browser cannot reach it and the recording-control API is
  not exposed to the LAN. To open the dashboard from another machine's
  browser:
  1. Set `SENSOR_DASHBOARD_HOST=0.0.0.0` (or a specific LAN IP) in the
     control PC's environment.
  2. Set `SENSOR_DASHBOARD_TOKEN` to a random secret — once set, `/api/start`
     and `/api/end` require it (as an `X-Auth-Token` header), and so do the
     `start_recording` / `stop_recording` commands on the `/ws` WebSocket.
  3. If the browser's URL uses a LAN IP rather than `localhost`, add that IP
     to `SENSOR_DASHBOARD_ALLOWED_HOSTS` (comma-separated) so it passes the
     Host-header allow-list. The same list also gates the WebSocket's `Origin`
     header, so a remote browser will not connect without this step.
  4. Open the dashboard once as `http://<host>:8000/?token=<secret>` so the
     page can authenticate its WebSocket; see [Dashboard security](#dashboard-security).
  5. Allow inbound TCP `8000` (dashboard) and `50001` (Pi sensor pushes)
     through the PC's firewall.

## Dashboard security

The dashboard can start and stop recordings and choose the directory CSV files
are written to, so the following protections apply to the `/ws` WebSocket as
well as to the HTTP API:

- **Host-header allow-list** (always on). `TrustedHostMiddleware` rejects
  requests whose `Host` is not `localhost`, `127.0.0.1`, `::1`, the configured
  bind address, or an entry of `SENSOR_DASHBOARD_ALLOWED_HOSTS`. This is the
  standard defence against DNS rebinding.
- **WebSocket `Origin` check** (always on). A `/ws` handshake is closed with
  code `1008` unless its `Origin` host is in that same allow-list. Browsers do
  not apply the same-origin policy to WebSockets, so without this check any web
  page the operator happens to visit could open `ws://localhost:8000/ws` and
  drive `update_config` + `start_recording` to write files to a directory of
  its choosing (cross-site WebSocket hijacking). A handshake with **no**
  `Origin` header is a non-browser client (curl, the flow runner, a test) and is
  accepted only from the local machine.
- **Token on recording control.** When `SENSOR_DASHBOARD_TOKEN` is set,
  `start_recording` / `stop_recording` over `/ws` require it — either as a
  `?token=<secret>` query parameter on the WebSocket URL or as a `"token"`
  field in the message — exactly as `POST /api/start` requires `X-Auth-Token`.
  When no token is configured (the localhost-only default), those two actions
  are restricted to local clients.
- **Local-only privileged actions** (always on). `update_config`,
  `pick_folder` and `open_folder` are refused for any non-loopback client
  regardless of the token.
- **Upload size limit.** `POST /api/upload_video` stops at 200 MB and returns
  HTTP 413, deleting the partial file; override with
  `SENSOR_DASHBOARD_MAX_UPLOAD_BYTES`.

The browser front end has no token input field. Open the dashboard once as
`http://<host>:8000/?token=<secret>` — `static/app.js` stores the value in
`localStorage` under the key `sensorDashboardToken` and reuses it on every
later visit and reconnect (you can also set that key from the browser console).
Rejected commands are reported back on the WebSocket as
`{"type": "error", "action": ..., "detail": ...}` and appear in the page's log
panel.
- The TCP sensor listener (`SENSOR_DASHBOARD_TCP_HOST`, default `0.0.0.0`)
  always listens on all interfaces, since the Pi is necessarily a separate
  host — there is no loopback-only mode for it.

## Running the dashboard on the PC

```bash
pip install -r src/monitoring/requirements.txt
python src/monitoring/dashboard/launch_sensor_dashboard.py
```

This calls `uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)` directly from the
script's `__main__` block (rather than the `uvicorn` CLI), using the host/port
resolved as described above. By default it also opens the dashboard in a
browser about 1.5 s after startup; set `SENSOR_DASHBOARD_HEADLESS=1` to skip
that (e.g. when running on a headless machine or under a process supervisor).

Port numbers come from `src/monitoring/config.yaml` if present, else from
[`config.example.yaml`](config.example.yaml), else from the built-in defaults
`web_port: 8000` / `tcp_port: 50001`. `config.yaml` is gitignored — copy
`config.example.yaml` to `config.yaml` in this directory to override the
ports locally without touching version control. Whatever `tcp_port` is
configured on the PC must match `SENSOR_AGENT_PC_PORT` in every Pi's
`/etc/sensor-agent.env`.

Recordings are written under `experiment_data/` at the repository root by
default; override with `SENSOR_DASHBOARD_DATA_DIR`.

## Log format

Each recording session produces up to three files in the save directory,
named from the session start time and an optional experiment-name prefix
(from `POST /api/start`'s `experiment_name`, sanitized to `[A-Za-z0-9_-]`):

- `{prefix}session_YYYYMMDD_HHMMSS.csv` — sensor readings
- `{prefix}session_YYYYMMDD_HHMMSS_tags.csv` — AprilTag poses
- `{prefix}session_YYYYMMDD_HHMMSS.webm` — optional, only if the browser's
  in-page webcam recorder was used (`POST /api/upload_video`)

The sensor CSV is written by sampling `latest_sensor_data` (the most recent
message received from each Pi) every 100 ms, regardless of each Pi's own
push rate — so a slow or bursty Pi still produces one row per tick, holding
its last known value. See [`examples/zif8/`](../../examples/zif8/) for sample
files (`sensor_session_sample.csv`, `sensor_session_sample_tags.csv`) showing
this layout; they are placeholders, not measurements from an actual run.

### Sensor CSV columns

| Column | Meaning |
|---|---|
| `timestamp` | ISO 8601, UTC, with offset (e.g. `2026-01-01T03:00:00.000000+00:00`) |
| `hostname` | Reporting Pi's hostname |
| `temperature` | °C |
| `humidity` | %RH; divided by 1024 (0-0.098) in every recording made before 2026-09-23, see [Measured quantities](#measured-quantities) |
| `lux` | Illuminance |
| `uvi` | UV index (float); `0` in every recording made before 2026-09-08, see [Measured quantities](#measured-quantities) |
| `voc_raw` | VOC, raw sensor signal |
| `acc_x`, `acc_y`, `acc_z` | Acceleration, g |
| `gyro_x`, `gyro_y`, `gyro_z` | Angular velocity, deg/s |

### Tags CSV columns

| Column | Meaning |
|---|---|
| `timestamp` | ISO 8601, UTC, with offset |
| `tag_id` | Detected AprilTag (36h11) ID |
| `tx_mm`, `ty_mm`, `tz_mm` | Translation relative to the camera, mm |
| `roll`, `pitch`, `yaw` | Rotation relative to the camera, degrees |
| `dx_mm`, `dy_mm`, `dz_mm` | Translation relative to this tag's zero reference, mm (blank if no zero has been set) |
| `droll`, `dpitch`, `dyaw` | Rotation relative to the zero reference, degrees (blank if no zero has been set) |
| `outlier` | `1` if this sample's pose jump was rejected and the previous smoothed pose was held instead, else `0` |

The camera's intrinsics are an approximation (`fx ≈ 0.85 × frame width`), so
absolute `t*_mm` values carry a distance-dependent bias; relative motion and
angles (and the zero-referenced `d*` columns) are the more reliable readout.

## Overhead camera recording

```python
from src.monitoring.camera.video_recorder import VideoRecorder

recorder = VideoRecorder(
    output_path="experiment_data/session_20260101_120000.mp4",
    camera_index=1,   # see config.yaml's video.camera_index
)
recorder.start()
# ... run the experiment ...
saved_path = recorder.stop()
```

`camera_index` is the OpenCV device index for the Logitech C920n mounted
above the setup; it is platform- and machine-dependent (probe with a short
OpenCV script if recordings come from the wrong camera). The recorder runs on
its own background thread and paces frames to the camera's own reported FPS.

## HTTP API

The dashboard exposes an HTTP API — used by the GUI (`src/gui/app.py`) and the flow runner to
start and stop recording, and available to any other program on the LAN to
read live sensor values. See [`dashboard/API.md`](dashboard/API.md) for the
full endpoint reference, request/response examples, and authentication
notes.
