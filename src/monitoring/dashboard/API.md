# Sensor Web Dashboard — HTTP API for external programs

## Overview

While the dashboard is running, other programs on the same PC or the same LAN
can read sensor values and control recording over HTTP.

- Host: `http://<IP of the PC running the dashboard>:8000`
  From the same PC: `http://localhost:8000`
- Format: JSON request and response bodies.
- Authentication: none by default (LAN-internal use is assumed). If the
  `SENSOR_DASHBOARD_TOKEN` environment variable is set on the PC, every
  state-changing `POST` (`/api/start`, `/api/end`, `/api/upload_video`,
  `/api/detect_tags`, `/api/zero_tags`, `/api/clear_zero`) requires a matching
  `X-Auth-Token` request header, and the WebSocket's recording commands
  require the same token — see [Authentication](#authentication) below. This
  is required whenever the dashboard is bound to anything other than
  `127.0.0.1`.
- Those six `POST` endpoints also refuse (HTTP 403) a browser request whose
  `Origin`/`Referer` is another site; clients that send neither header are
  unaffected.

## Endpoints

### `GET /api/status`

Current dashboard state.

```json
{
  "is_recording": false,
  "device_count": 2,
  "devices": ["192.0.2.10", "192.0.2.11"],
  "save_dir": "experiment_data"
}
```

### `GET /api/sensors`

Latest sensor reading from every connected Pi (dict keyed by Pi IP address).

```json
{
  "192.0.2.10": {
    "type": "sensor_data",
    "temp": 24.3, "humi": 55.2,
    "accel": [0.01, -0.02, 9.81],
    "gyro":  [0.0, 0.0, 0.0],
    "lux_raw": 320, "uv": 0.42, "voc": 30000,
    "ok": {"bme280": true, "tsl25911": true, "icm20948": true, "ltr390": true, "sgp40": true},
    "model": "BME280 (T/H)",
    "ip": "192.0.2.10",
    "hostname": "raspberrypi.local",
    "received_at": "2026-01-01T03:00:00.123456+00:00"
  }
}
```

Any reading can be `null` (a failed read on the Pi; the matching `ok` flag is
then `false`). `lux_raw` is the TSL25911 channel-0 raw count, not lux; it was
called `lux` before 2026-09-23. `received_at` is when the dashboard received
this sample.

### `GET /api/sensors/{ip}`

Latest reading from a single Pi. Substitute the Pi's IP address for `{ip}`.
Returns HTTP 404 if no data has been received for that address yet.

### `POST /api/start`

Start recording. Optional JSON request body:

```json
{"experiment_name": "exp_001"}
```

If `experiment_name` is given, it is used as the filename prefix for the CSV
files. Returns HTTP 409 if a recording is already in progress. Requires the
`X-Auth-Token` header if `SENSOR_DASHBOARD_TOKEN` is set (see
[Authentication](#authentication)).

```json
{
  "status": "started",
  "experiment_name": "exp_001",
  "save_dir": "experiment_data"
}
```

### `POST /api/end`

Stop recording. The CSV files are saved automatically and the save path is
returned. Returns HTTP 409 if no recording is in progress. Requires the
`X-Auth-Token` header if `SENSOR_DASHBOARD_TOKEN` is set.

```json
{
  "status": "ended",
  "saved_path": "experiment_data/session_20260101_120000.csv"
}
```

### Other endpoints

These are used mainly by the dashboard's own browser front end, but are
plain HTTP/JSON and can be called by any client:

| Method / path | Purpose |
|---|---|
| `POST /api/detect_tags` | AprilTag pose estimation from a single uploaded camera frame (appends to the tags CSV while recording) |
| `POST /api/zero_tags` | Capture the currently-tracked AprilTag poses as zero references |
| `POST /api/clear_zero` | Clear the zero references |
| `POST /api/upload_video` | Save a browser-recorded (webm) video clip to the current save directory. Bodies over 200 MB (`SENSOR_DASHBOARD_MAX_UPLOAD_BYTES`) are rejected with HTTP 413 |
| `GET /api/is_safe` (also `GET /is_safe`) | Liveness check, always `{"safe": true}` |

All four need `X-Auth-Token` when `SENSOR_DASHBOARD_TOKEN` is set and refuse
cross-origin browser requests, like `/api/start` and `/api/end`. See the
interactive docs (below) for their exact request/response schemas.

## WebSocket `/ws`

The dashboard's own front end uses `ws://<host>:8000/ws` for live sensor
broadcasts and for recording control. Two rules apply to every connection:

- **`Origin` check.** The handshake is closed with code `1008` unless the
  `Origin` header's host is in the allow-list used by the Host-header check
  (`localhost`, `127.0.0.1`, `::1`, the bind address, plus
  `SENSOR_DASHBOARD_ALLOWED_HOSTS`). This blocks cross-site WebSocket
  hijacking — the same-origin policy does not cover WebSockets, so any page in
  the operator's browser could otherwise drive this endpoint. A handshake with
  no `Origin` at all is a non-browser client and is accepted only from the
  local machine.
- **Token on recording control.** When `SENSOR_DASHBOARD_TOKEN` is set, the
  `start_recording` and `stop_recording` commands need the token; without it
  the server replies `{"type": "error", "action": ..., "detail": "invalid or
  missing auth token"}` and does nothing. Pass it either at connect time as a
  query parameter or per message:

```
ws://localhost:8000/ws?token=<value of SENSOR_DASHBOARD_TOKEN>
```

```json
{"action": "start_recording", "token": "<value of SENSOR_DASHBOARD_TOKEN>"}
```

  When no token is configured, those two commands are accepted only from
  loopback clients. `update_config`, `pick_folder` and `open_folder` are always
  loopback-only, token or not.

The browser front end reads the token from a `?token=` query parameter on the
dashboard URL and remembers it in `localStorage` under `sensorDashboardToken`.

## Authentication

Set `SENSOR_DASHBOARD_TOKEN` in the dashboard PC's environment before
starting `launch_sensor_dashboard.py` to require a token on `/api/start`,
`/api/end`, `/api/upload_video`, `/api/detect_tags`, `/api/zero_tags` and
`/api/clear_zero`:

```
X-Auth-Token: <value of SENSOR_DASHBOARD_TOKEN>
```

Requests without a valid token receive HTTP 401. The same token gates
`start_recording` / `stop_recording` on the WebSocket (see above). This is
unrelated to the Host-header allow list
(`SENSOR_DASHBOARD_ALLOWED_HOSTS`), which is a separate, always-on protection
against DNS rebinding — though the WebSocket `Origin` check reuses that same
host list.

Independently of the token, those six endpoints check `Origin` (or, if it is
absent, `Referer`): a value that is not this dashboard's own
`scheme://host:port` gets HTTP 403. This stops another web site open in the
operator's browser from, for example, submitting a form to `/api/end`. With no
token configured, any local process (or LAN host, if the server is bound to the
LAN) that sends neither header can still call these endpoints — set a token if
that matters.

## Example calls

### curl

```bash
curl http://localhost:8000/api/status

curl http://localhost:8000/api/sensors

curl -X POST http://localhost:8000/api/start \
     -H "Content-Type: application/json" \
     -d '{"experiment_name":"exp_001"}'

curl -X POST http://localhost:8000/api/end
```

With a token set:

```bash
curl -X POST http://localhost:8000/api/start \
     -H "Content-Type: application/json" \
     -H "X-Auth-Token: $SENSOR_DASHBOARD_TOKEN" \
     -d '{"experiment_name":"exp_001"}'
```

### Python (`requests`)

```python
import requests

BASE = "http://localhost:8000"

# Current status
print(requests.get(f"{BASE}/api/status").json())

# All sensor values
data = requests.get(f"{BASE}/api/sensors").json()
for ip, msg in data.items():
    print(ip, msg["temp"], msg["humi"])

# Start recording
r = requests.post(f"{BASE}/api/start", json={"experiment_name": "exp_001"})
print(r.json())

# Stop recording
r = requests.post(f"{BASE}/api/end")
print(r.json())
```

## Interactive testing (Swagger UI)

Open the following URL in a browser to call every endpoint from a GUI and see
the response immediately:

```
http://localhost:8000/docs
```

## Notes

- The API only responds while the dashboard process is running.
- To call it from another PC on the LAN, allow inbound traffic on port 8000
  in the firewall, bind the dashboard to a non-loopback address
  (`SENSOR_DASHBOARD_HOST`), add that address to
  `SENSOR_DASHBOARD_ALLOWED_HOSTS` if needed, and set
  `SENSOR_DASHBOARD_TOKEN`.
- When `/api/start` is called by an external program, the web UI also picks
  up the "recording" state automatically over its WebSocket connection.
- Polling the same data at a short interval adds negligible load, but there
  is no benefit to polling faster than every 100 ms — that is also the
  dashboard's own sampling interval.
