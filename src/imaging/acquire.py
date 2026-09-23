#!/usr/bin/env python3
"""3-wavelength spectral capture and analysis.

Cycles the NEEWER light through white -> red -> green -> blue and captures a
photo at each step, metering each monochromatic LED on the camera's
"secondary" channel (the one that does not saturate), and computes a
depth-resolved optical density (OD) profile for each of the three
wavelengths.

  - A white (CCT) shot is used to detect geometry (liquid level, tube body
    position, etc.), which is then reused for the monochromatic shots
  - Each color's secondary-channel brightness is checked and, if it is
    saturated or too dim, the LED intensity is adjusted and the shot retaken
  - The camera's white balance is fixed to manual 4600 K and kept there
    (switching auto <-> manual produces an irreproducible color state, so it
    is never switched back)
  - Captured photos are moved into their own run directory (monochromatic
    shots are not compatible with the standard luminance-based geometry
    detector, so they are kept separate)

Output (in the run directory):
  ref_white.jpg, shot_red.jpg, shot_green.jpg, shot_blue.jpg
  profiles.csv   per-row OD (od_625 / od_525 / od_465)
  spectral.png   3-wavelength OD-vs-depth figure
  summary.json   geometry, LED intensity, sedimentation front, etc.

Usage:
    python3 acquire.py                  # capture -> analyze, unnamed run
    python3 acquire.py --intensity 20   # initial monochromatic LED intensity (%)
    python3 acquire.py --restore 1 5600 # light state to leave after finishing
                                         # (default: off)
    python3 acquire.py --folder expA --name vial1
                                         # save as a named run under
                                         # <PHOTOS_DIR>/expA/vial1/
    python3 acquire.py --mode white --folder expA --name vial1
                                         # quick white-only measurement (a
                                         # single white shot gives R/G/B
                                         # channel OD; named runs only)
    python3 acquire.py --mode white --white-intensity 20 --folder expA --name vial1
                                         # fix the white LED intensity too
                                         # (for photo comparison / absolute
                                         # transmittance comparison across
                                         # samples; ref_white.jpg is always
                                         # captured at exactly this intensity)
"""
from __future__ import annotations   # NeewerLight type hints stay strings (see the lazy imports below)

import argparse
import csv
import json
import math
import secrets
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import hw
from geometry import extract_features

# Windows: when stdout is a pipe/file, the locale encoding becomes cp932 and
# the "warning/check-mark" prints raise UnicodeEncodeError, aborting the
# whole measurement. Replace characters that cannot be encoded instead.
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
import paths
from paths import OUT_DIR, PHOTOS_DIR, ensure_dirs

# The hardware layer (`hw`, `neewer_light`, `capture`) is imported lazily,
# inside the functions that actually talk to the light or the camera.
# Importing it at module level would pull in the BLE stack (pyobjc on macOS,
# bleak/winrt on Windows), which is neither installed nor needed on a
# machine that only re-analyses saved runs -- make_panel.py imports
# `od_profile` from this module and must work with numpy/Pillow/matplotlib
# alone. Every capture path below imports what it needs on first use.

# WB is always kept at manual 4600 K (never restored to auto). The C920's
# white balance has slow internal hidden state, and each time you switch
# between auto and manual it settles into a different color state. The only
# reproducible way to run it, confirmed experimentally, is to fix it to
# manual once and never touch it again.
WB_FIXED = 4600
WB_SETTLE_S = 5.0        # settle time right after switching to manual (from auto)
LED_SETTLE_S = 1.8       # settle time after changing the light setting

# (name, HSI hue, nominal wavelength nm, dominant channel, secondary channel
#  used for metering, curve color)
# secondary channel: the dominant channel saturates easily for a
# monochromatic LED, so metering is done on the neighboring channel that
# receives the same monochromatic light weakly (combination verified
# experimentally)
COLORS = [
    ("red",   0,   625, 0, 1, (255, 90, 80)),
    ("green", 120, 525, 1, 2, (90, 220, 110)),
    ("blue",  240, 465, 2, 1, (110, 160, 255)),
]

SEC_MIN, SEC_MAX = 12.0, 200.0   # acceptable secondary-channel brightness (sRGB) just below the meniscus
SEC_TARGET = 150.0               # target brightness (sRGB) for intensity adjustment
# If the dominant channel exceeds this, sensor saturation is suspected: some
# of it bleeds into the secondary channel through the color matrix (this
# added stray light is removed by od_profile's floor detection as additive
# stray light). If it still exceeds this even at minimum intensity, it is
# recorded as a warning.
# Note: changing the camera exposure live does not affect the running stream,
# so that option is not available here.
DOM_MAX = 235.0
MAX_RETRY = 4

WHITE_FIXED_DEFAULT = 20   # default white-only fixed intensity % (UI default / CLI help hint)

# Exclusion lock for the measurement sequence. If a periodic capture loop and
# a manual measurement run at the same time they fight over the light and
# camera and both measurements break, so they must always be serialized.
LOCK_PATH = paths.SPECTRAL_LOCK
LOCK_WAIT_S = 300

# Fixed field of view (locked geometry). When swapping samples for
# comparison, detecting geometry fresh every time can shift the field of
# view or fail outright, so geometry can be detected once and saved, then
# applied to every sample with the same frame. A single-vial periodic
# capture (tracking one vial's own drift) still uses fresh auto-detection as
# before and does not use this lock.
LOCK_GEO_PATH = OUT_DIR / "locked_geometry.json"
LOCK_GEO_REF = OUT_DIR / "locked_geometry_ref.jpg"


def validate_component(s: str) -> str:
    """Validate that s is safe to use as a single path component (folder/run name)."""
    if (not s or s in (".", "..") or s.startswith(".")
            or any(c in s for c in "/\\\0") or s != Path(s).name):
        raise ValueError(f"Not a valid folder/run name: {s!r}")
    return s


def _unique_dir(path: Path) -> Path:
    """Avoid overwriting an existing non-empty directory by appending a suffix if needed."""
    def usable(p: Path) -> bool:
        return not p.exists() or (p.is_dir() and not any(p.iterdir()))
    if usable(path):
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.name}_{i}")
        if usable(cand):
            print(f"{path} already exists; saving to {cand.name} instead")
            return cand
    raise RuntimeError(f"Could not find a free output name for: {path}")


def srgb_to_linear(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64) / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)




# ---------------------------------------------------------------- capture

def _geometry_ok(feat: dict) -> bool:
    """Sanity-check the detected geometry for physical plausibility.

    An overexposed image detects the meniscus lower than it really is, so
    require the liquid height to be substantial (assuming operation around a
    50% fill fraction).
    """
    men = feat["meniscus"]["y"]
    cap1 = feat["cap"]["y1"]
    bot = feat["liquid"]["bottom_y"]
    h = feat["image_size"]["height"]
    return cap1 < men < bot and (bot - men) > h * 0.35


def _normalize_body(feat: dict) -> None:
    """If the detected body column range looks suspicious, replace it with a
    45%-wide band centered on the vial.

    In dark images, body detection can end up including the glow band beside
    the tube.
    """
    if feat["body"]["width_px"] >= 0.7 * feat["vial"]["width_px"]:
        cx = (feat["vial"]["x0"] + feat["vial"]["x1"]) / 2
        half = 0.225 * feat["vial"]["width_px"]
        feat["body"] = {"x0": int(cx - half), "x1": int(cx + half),
                        "width_px": int(2 * half)}


def capture_white_reference(
        light: NeewerLight, run_dir: Path) -> tuple[Path, dict, int]:
    """Shoot under white light and detect geometry (liquid level, body position).

    Tries intensities from dark to bright, treating overexposure (>50%
    saturation in the center) as too bright and a failed/invalid geometry
    detection as too dim, moving to the next intensity in either case. The
    first intensity that succeeds is used, and its % is returned. At low
    exposure the white shot is dim so a higher intensity is needed; at high
    exposure a low intensity is enough.
    """
    from capture import capture_photo

    for brightness in (1, 3, 10, 20, 30, 50):
        light.set_cct(brightness, 5600)
        time.sleep(LED_SETTLE_S)
        shot = capture_photo()
        gray = np.asarray(Image.open(shot).convert("L"), dtype=np.float64)
        w = gray.shape[1]
        sat_frac = (gray[:, w // 3:2 * w // 3] >= 252).mean()
        if sat_frac > 0.5:
            print(f"  white {brightness}%: overexposed (saturated fraction {sat_frac:.0%}) -> trying another intensity")
            shot.unlink()
            continue
        try:
            feat = extract_features(shot)
        except ValueError as e:
            print(f"  white {brightness}%: detection failed ({e}) -> trying another intensity")
            shot.unlink()
            continue
        if not _geometry_ok(feat):
            print(f"  white {brightness}%: invalid geometry "
                  f"(meniscus y={feat['meniscus']['y']}) -> trying another intensity")
            shot.unlink()
            continue
        _normalize_body(feat)
        print(f"  white {brightness}%: OK (saturated fraction {sat_frac:.0%})")
        dst = run_dir / "ref_white.jpg"
        shutil.move(shot, dst)
        return dst, feat, brightness
    raise RuntimeError("Could not detect geometry from any white reference image")


def capture_white_image(light: NeewerLight, run_dir: Path,
                        brightness: int) -> Path:
    """Capture a single white reference image without geometry detection (for locked framing)."""
    from capture import capture_photo

    light.set_cct(brightness, 5600)
    time.sleep(LED_SETTLE_S)
    shot = capture_photo()
    dst = run_dir / "ref_white.jpg"
    shutil.move(shot, dst)
    return dst


def _white_geometry_from_fixed_shot(light: NeewerLight, run_dir: Path,
                                    ref_path: Path, white_intensity: int) -> dict:
    """Try geometry detection on the fixed-intensity white image first, and if
    that fails, fall back to an auto-intensity sweep used for detection only.

    The photometry target (ref_white.jpg = ref_path) is an invariant and is
    never rewritten.
    """
    try:
        feat = extract_features(ref_path)
        if not _geometry_ok(feat):
            raise ValueError(f"meniscus y={feat['meniscus']['y']}")
    except ValueError as e:
        print(f"  fixed intensity {white_intensity}%: geometry detection failed ({e}) -> "
              f"falling back to an auto-intensity sweep for geometry only "
              f"(the photometry image stays at the fixed {white_intensity}%)")
        # Move ref_path aside so it doesn't collide with the same-named path
        # that capture_white_reference() writes to. Only the geometry is
        # kept from the sweep; the sweep image itself is discarded.
        fixed_shot = run_dir / "_ref_white_fixed.jpg"
        ref_path.rename(fixed_shot)
        try:
            sweep_path, feat, _sweep_brt = capture_white_reference(light, run_dir)
            sweep_path.unlink()
        finally:
            fixed_shot.rename(ref_path)
        return feat
    _normalize_body(feat)
    print(f"  geometry: meniscus y={feat['meniscus']['y']}, "
          f"body x={feat['body']['x0']}-{feat['body']['x1']}, "
          f"liquid bottom y={feat['liquid']['bottom_y']} "
          f"(detected from the fixed-intensity {white_intensity}% image)")
    return feat


def _fix_white_balance() -> dict:
    """Fix the white balance to manual 4600 K (wait out the transition if it wasn't already fixed).

    Camera control goes through the server (the process that owns the
    camera), over HTTP. On Windows, DirectShow holds the camera exclusively,
    so this is the only available path (the same path also works on macOS).
    autoWhiteBalance is True=auto, whiteBalance is in Kelvin (both are the
    control names exposed by the camera server).

    Every write is checked (capture.set_control raises on failure) and the
    two values are read back afterwards; a read-back that differs from
    manual/WB_FIXED raises RuntimeError. Returns the read-back values
    {"autoWhiteBalance": ..., "whiteBalance": ...}.
    """
    from capture import get_control, set_control

    wb_auto = get_control("autoWhiteBalance")
    wb_temp = get_control("whiteBalance")
    already = wb_auto is False and wb_temp == WB_FIXED
    set_control("autoWhiteBalance", False)
    set_control("whiteBalance", WB_FIXED)
    if not already:
        print(f"Fixing WB: auto={wb_auto} temp={wb_temp} -> manual {WB_FIXED} (waiting for transition)")
        time.sleep(WB_SETTLE_S)
    read_back = {"autoWhiteBalance": get_control("autoWhiteBalance"),
                 "whiteBalance": get_control("whiteBalance")}
    diff = {k: v for k, v in read_back.items()
            if v != {"autoWhiteBalance": False, "whiteBalance": WB_FIXED}[k]}
    if diff:
        raise RuntimeError(
            f"White balance did not stick: read back {read_back} "
            f"(requested manual {WB_FIXED} K)")
    return read_back


def camera_preflight() -> dict:
    """Query the camera server's /api/health and refuse to measure on a
    stream that cannot be trusted.

    Aborts (RuntimeError) when the stream is stale -- camera settings were
    changed after the stream started, so frames still use the old ones
    (README rule 2) -- or when there is no fresh frame / no live video
    source / the controls cannot be read. A mismatch against
    camera_reference.json does not abort (the reference depends on the
    sample; see that file) but is returned and recorded in summary.json.

    Returns the per-run camera record: the READ-BACK settings (logical
    names), the device actually held open, and the reference comparison.
    """
    from capture import get_health

    h = get_health()
    if h.get("stream_settings_stale"):
        raise RuntimeError(
            "The camera stream is stale: settings were changed after the "
            "stream started, so photos would still use the old ones. "
            "Restart camera_server.py and try again (README rule 2).")
    problems = []
    if not (h.get("frame") or {}).get("fresh"):
        problems.append("no fresh frame")
    if not (h.get("ffmpeg") or {}).get("alive"):
        problems.append("video source not running")
    cam = h.get("camera") or {}
    if not cam.get("connected"):
        problems.append("camera controls unreadable")
    if problems:
        raise RuntimeError(f"Camera server not ready: {', '.join(problems)}")
    if cam.get("matches_reference") is False:
        print(f"  [warning] camera differs from camera_reference.json: "
              f"{cam.get('mismatches')}")
    return {"settings": cam.get("settings"),
            "device": cam.get("device"),
            "matches_reference": cam.get("matches_reference"),
            "mismatches": cam.get("mismatches") or []}


@contextmanager
def run_owner():
    """Register this run as the owner of the camera controls.

    Must be entered while holding LOCK_PATH. Writes a random token to
    paths.RUN_OWNER_PATH and attaches it to this process's control writes;
    camera_server.py refuses /api/control writes without it while the lock
    is held. The token file is removed on exit.
    """
    import capture

    token = secrets.token_hex(16)
    paths.RUN_OWNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.RUN_OWNER_PATH.write_text(token, encoding="utf-8")
    capture.set_run_token(token)
    try:
        yield token
    finally:
        capture.set_run_token(None)
        try:
            if paths.RUN_OWNER_PATH.read_text(encoding="utf-8").strip() == token:
                paths.RUN_OWNER_PATH.unlink()
        except OSError:
            pass


def restore_light(light, restore: tuple[int, int]) -> str:
    """Leave the light in the requested state and always close the handle.

    Brightness 0 means off: dim first (so it is not blinding when next
    powered on), then power off. Nested try/finally: power(False) runs
    even if the dimming command fails, and close() runs even if both do;
    the first error is re-raised afterwards.
    """
    try:
        if restore[0] <= 0:
            try:
                light.set_cct(1, restore[1])
            finally:
                light.power(False)
            return "off"
        light.set_cct(*restore)
        return f"CCT {restore[0]}%/{restore[1]}K"
    finally:
        light.close()


def load_locked_geometry() -> dict:
    """Load the saved locked geometry. Raises RuntimeError if none exists."""
    if not LOCK_GEO_PATH.is_file():
        raise RuntimeError(
            f"No locked geometry is set. Run with `--calibrate` first "
            f"({LOCK_GEO_PATH})")
    return json.loads(LOCK_GEO_PATH.read_text(encoding="utf-8"))


def calibrate_geometry(restore: tuple[int, int] = (0, 5600)) -> int:
    """Shoot a white reference, detect geometry, and save it as the locked field of view.

    Run this once before comparing samples. If the detected body width is
    abnormally small (detection breakdown), it is not saved and an error is
    raised instead (so a 1px-wide misdetection never gets locked in).
    """
    import hw
    from neewer_light import NeewerLight

    with hw.file_lock(
            LOCK_PATH, LOCK_WAIT_S,
            on_wait=lambda: print("Another measurement is running; waiting for it to finish...")), \
            run_owner():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp = OUT_DIR / f".calib_{ts}"
        tmp.mkdir(parents=True, exist_ok=True)
        light = NeewerLight()
        print(f"Light: {light.name}")
        try:
            light.power(True)
            light.set_cct(1, 5600)
            time.sleep(LED_SETTLE_S)
            _fix_white_balance()
            camera_preflight()
            print("Capturing white reference (calibration)...")
            ref_path, geo, white_brt = capture_white_reference(light, tmp)
        finally:
            restore_light(light, restore)
        body_w = geo["body"]["x1"] - geo["body"]["x0"]
        vial_w = geo["vial"]["x1"] - geo["vial"]["x0"]
        if body_w < 0.3 * vial_w:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(
                f"Body detection looks wrong (width {body_w}px / vial width {vial_w}px). "
                "Check the vial's position and the white light intensity, then try again.")
        shutil.move(ref_path, LOCK_GEO_REF)
        shutil.rmtree(tmp, ignore_errors=True)
        LOCK_GEO_PATH.write_text(json.dumps(
            {"geometry": geo, "white_brightness": white_brt,
             "calibrated_at": ts}, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"\nSaved locked geometry: {LOCK_GEO_PATH}")
        print(f"  meniscus y={geo['meniscus']['y']}, "
              f"body x={geo['body']['x0']}-{geo['body']['x1']} (width {body_w}px), "
              f"liquid bottom y={geo['liquid']['bottom_y']}, white intensity {white_brt}%")
        print(f"  reference image: {LOCK_GEO_REF}")
        return 0


def capture_color(light: NeewerLight, run_dir: Path, name: str, hue: int,
                  dom_ch: int, sec_ch: int, geo: dict,
                  intensity: int, fixed: bool = False) -> tuple[Path, int, str | None]:
    """Shoot under monochromatic light, adjusting intensity until the secondary channel is well exposed.

    Saturation of the dominant channel (above DOM_MAX) can be unavoidable
    even at minimum intensity, in which case it is recorded as a warning
    (od_profile's stray-light floor detection corrects for it). With
    fixed=True, intensity adjustment is skipped for cross-sample absolute
    comparison, and a single shot is taken at the given intensity.
    """
    from capture import capture_photo

    men_y = geo["meniscus"]["y"]
    x0, x1 = geo["body"]["x0"], geo["body"]["x1"]
    for attempt in range(MAX_RETRY):
        light.set_hsi(hue, 100, intensity)
        time.sleep(LED_SETTLE_S)
        shot = capture_photo()
        rgb = np.asarray(Image.open(shot).convert("RGB"), dtype=np.float64)
        band = rgb[men_y + 10:men_y + 40, x0:x1]
        v = float(band[..., sec_ch].mean())
        dom_v = float(band[..., dom_ch].mean())
        clip = float((band[..., sec_ch] >= 254).mean())
        print(f"  {name}: intensity {intensity}% -> secondary ch {v:.0f} / dominant ch {dom_v:.0f}",
              end="")
        if attempt < MAX_RETRY - 1 and (v > SEC_MAX or v < SEC_MIN) and not fixed:
            # estimate the intensity needed to hit the target from the
            # linear-intensity ratio
            scale = srgb_to_linear(SEC_TARGET) / max(srgb_to_linear(v), 1e-4)
            new = int(np.clip(round(intensity * scale), 1, 100))
            if v > SEC_MAX and intensity > 1:
                intensity = min(new, intensity - 1)
                print(f" -> adjusting intensity to {intensity}%")
                shot.unlink()
                continue
            if v < SEC_MIN and intensity < 100:
                intensity = max(new, intensity + 1)
                print(f" -> adjusting intensity to {intensity}%")
                shot.unlink()
                continue
        warn = None
        if dom_v > DOM_MAX or clip > 0.01:
            warn = f"Dominant channel is near saturation (dom={dom_v:.0f}, clip={clip:.1%})"
        elif v > SEC_MAX:
            warn = f"Secondary channel brightness too high (v={v:.0f})"
        elif v < SEC_MIN:
            warn = f"Secondary channel brightness too low (v={v:.0f})"
            if fixed:
                warn += " (intensity is not raised in fixed-intensity mode, so the lower OD range may be lost)"
        print(" [ok]" if warn is None else f" [warning] {warn}")
        dst = run_dir / f"shot_{name}.jpg"
        shutil.move(shot, dst)
        return dst, intensity, warn
    raise RuntimeError(f"{name}: intensity adjustment did not converge")


# ---------------------------------------------------------------- analysis

def _finite(v):
    """float(v), or None if it is NaN/inf (summary.json must stay valid JSON)."""
    if v is None:
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _od_curve(sig: np.ndarray, y_top: int, cut: float):
    """OD = -log10(sig / I0) row by row until sig falls to the cut.

    I0 = mean of the first 15 rows. Returns (ys, ods, extinct_y, i0). With
    no usable I0 (i0 <= cut: the image is dark, or all signal is below the
    pedestal) the curve is empty and extinct_y is y_top -- never NaN/inf.
    """
    i0 = float(sig[:15].mean()) if len(sig) else 0.0
    if not math.isfinite(i0) or i0 <= cut:
        return [], [], y_top, i0
    ys, ods = [], []
    extinct_y = None
    for i, v in enumerate(sig):
        if v <= cut:
            extinct_y = y_top + i
            break
        ys.append(y_top + i)
        ods.append(float(-np.log10(v / i0)))
    return ys, ods, extinct_y, i0


MIN_LIQUID_ROWS = 20


def _check_bounds(geo: dict, h: int, w: int) -> None:
    """Raise ValueError if the geometry does not fit the image."""
    men_y = geo["meniscus"]["y"]
    bottom = geo["liquid"]["bottom_y"] - 22
    x0, x1 = geo["body"]["x0"], geo["body"]["x1"]
    cap_y0 = geo["cap"]["y0"]
    trim = (x1 - x0) * 20 // 100
    problems = []
    if not 0 <= cap_y0 < h:
        problems.append(f"cap y0={cap_y0} outside 0..{h - 1}")
    if not 0 <= men_y < h:
        problems.append(f"meniscus y={men_y} outside 0..{h - 1}")
    if not men_y + 8 + MIN_LIQUID_ROWS <= bottom <= h:
        problems.append(f"liquid rows {men_y + 8}..{bottom} empty, too short "
                        f"(< {MIN_LIQUID_ROWS}) or beyond the image height {h}")
    if not (0 <= x0 < x1 <= w) or (x1 - trim) - (x0 + trim) < 1:
        problems.append(f"body columns {x0}..{x1} invalid for width {w}")
    if problems:
        raise ValueError("Geometry does not fit the image: " + "; ".join(problems))


def od_profile(path: Path, sec_ch: int, geo: dict) -> dict:
    """Compute the depth-resolved OD from the secondary channel.

    The pedestal (lens flare + dark current) is normally estimated from the
    dark background above the cap. When extinction is deep enough that the
    signal pins to the flare floor, a "flat minimum-value window" within the
    liquid is instead adopted as the floor, and rows beyond where the signal
    reaches that floor are truncated (only a lower bound on OD is known there).

    Returns ys/ods (the corrected profile), extinct_y, pedestal,
    pedestal_mode ("cap" or "floor"), pedestal_rule ("cap", "flat_window"
    or "min_then_rise": which rule set the pedestal), i0, and the
    uncorrected profile ys_cap/ods_cap/i0_cap computed with the cap
    pedestal alone (identical to ys/ods when pedestal_mode is "cap"), plus
    warning (None or text). The min-then-rise rule is a heuristic, so a
    warning is attached whenever it chose the floor. A uniformly dark
    image gives empty curves and a warning, never NaN/inf. Geometry that
    does not fit the image raises ValueError.
    """
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)
    h, w = rgb.shape[:2]
    _check_bounds(geo, h, w)
    men_y = geo["meniscus"]["y"]
    bottom = geo["liquid"]["bottom_y"] - 22     # exclude the bright band at the bottom
    x0, x1 = geo["body"]["x0"], geo["body"]["x1"]
    cap_y0 = geo["cap"]["y0"]
    # avoid light piped along the glass wall by metering only the inner 60%
    # of the body's columns
    trim = (x1 - x0) * 20 // 100
    xi0, xi1 = x0 + trim, x1 - trim

    lin = srgb_to_linear(rgb[:, xi0:xi1, sec_ch]).mean(axis=1)
    smooth = np.convolve(lin, np.ones(9) / 9, mode="same")

    d0 = max(0, cap_y0 - 70)
    ped_cap = float(lin[d0:max(d0 + 10, cap_y0 - 20)].mean())

    y_top = men_y + 8
    liq = smooth[y_top:bottom]

    # In a clear supernatant, there is an optical dip just below the meniscus
    # (the curved liquid surface refracts light and darkens it), and
    # transmitted light peaks in the clear layer just below that. The
    # unattenuated transmitted light I0 is referenced to this peak, and the
    # depth analysis also starts from the peak (this avoids misreading the
    # rise out of the dip as a "re-rise from bottom glow"). For a turbid
    # sample the peak falls right at the meniscus (index 0), so the guard
    # below resets peak_i to 0 and reproduces the old behavior.
    head = liq[:max(15, len(liq) // 4)]
    peak_i = int(head.argmax())
    if liq[peak_i] <= 1.5 * float(liq[:5].mean()):
        peak_i = 0   # if there is no clear dip, use the meniscus itself as before
    y_top += peak_i
    liq = liq[peak_i:]
    i0_raw = float(liq[:15].mean()) - ped_cap

    # Stray-light floor detection. True transmitted light should decrease
    # monotonically with depth, so:
    #  (a) a flat, low 80-row window (signal pinned to the flare floor after
    #      full extinction)
    #  (b) the minimum value inside the liquid, past which attenuation
    #      stalls or reverses (the depth where stray light such as bottom
    #      glow starts to dominate)
    # are both treated as the flare/stray-light floor and adopted as the pedestal.
    floor, rule = None, None
    for s in range(0, len(liq) - 80, 20):
        win = liq[s:s + 80]
        if win.std() < 0.05 * win.mean() and win.mean() < 0.3 * (i0_raw + ped_cap):
            if floor is None or win.mean() < floor:
                floor, rule = float(win.mean()), "flat_window"
    run_min = np.minimum.accumulate(liq)
    exceed = liq > run_min * 1.3
    count, onset = 0, None
    for i, e in enumerate(exceed):
        count = count + 1 if e else 0
        if count >= 10:
            onset = i - 9
            break
    if onset is not None and run_min[onset] < 0.35 * (i0_raw + ped_cap):
        if floor is None or float(run_min[onset]) > floor:
            floor, rule = float(run_min[onset]), "min_then_rise"
    use_floor = floor is not None and floor > 2 * ped_cap
    ped = floor if use_floor else ped_cap
    rule = rule if use_floor else "cap"

    warnings = []
    ys, ods, extinct_y, i0 = _od_curve(liq - ped, y_top, max(0.35 * ped, 1e-4))
    ys_cap, ods_cap, _ext_cap, i0_cap = _od_curve(
        liq - ped_cap, y_top, max(0.35 * ped_cap, 1e-4))
    if not ys:
        warnings.append(f"No transmitted signal above the pedestal "
                        f"(I0={i0:.3g}, pedestal={ped:.3g}); profile is empty")
    if rule == "min_then_rise":
        warnings.append(
            f"Pedestal chosen by the minimum-then-rise rule (floor at "
            f"y={y_top + onset}); the floor choice is heuristic and OD past "
            f"it is a lower bound -- compare with the cap-pedestal profile")
    return {"ys": ys, "ods": ods, "extinct_y": extinct_y,
            "pedestal": _finite(ped),
            "pedestal_mode": "floor" if use_floor else "cap",
            "pedestal_rule": rule,
            "i0": _finite(i0),
            "ys_cap": ys_cap, "ods_cap": ods_cap, "i0_cap": _finite(i0_cap),
            "pedestal_cap": _finite(ped_cap),
            "warning": "; ".join(warnings) or None}


def _empty_profile(reason: str) -> dict:
    """Profile placeholder when od_profile cannot run (e.g. bad geometry)."""
    return {"ys": [], "ods": [], "extinct_y": None, "pedestal": None,
            "pedestal_mode": None, "pedestal_rule": None, "i0": None,
            "ys_cap": [], "ods_cap": [], "i0_cap": None, "pedestal_cap": None,
            "warning": reason}


def safe_od_profile(path: Path, sec_ch: int, geo: dict) -> dict:
    """od_profile, but a geometry that does not fit the image gives an empty
    profile with a warning instead of aborting the run (the photos are
    already taken; summary.json must still be written)."""
    try:
        return od_profile(path, sec_ch, geo)
    except ValueError as e:
        return _empty_profile(str(e))


def white_headspace_stats(
        ref_path: Path, geo: dict) -> tuple[list[float] | None, float | None, int]:
    """Measured statistics for the headspace band (cap bottom to meniscus) of ref_white.jpg.

    This band is illuminated directly by the backlight without passing
    through the sample, so regardless of sample turbidity it reflects only
    the light's actual effective intensity. (This addresses the "the
    recorded intensity can lie" problem: even if a BLE command silently
    fails to take effect, the recorded intensity_pct still shows the
    commanded value, so this value lets you cross-check after the fact
    whether multiple runs' illumination was really matched.) Always computed
    regardless of mode / white_intensity.
    The y-range matches geometry.py's headspace definition (cap bottom + 1
    to meniscus - 1). The x-trim follows the same rule as od_profile (inner
    60% of the body, excluding light piped along the wall).

    Returns (mean, clip, height_px):
      mean: average R/G/B brightness ([R, G, B], None if it cannot be computed)
      clip: fraction of saturated pixels (brightness >= 254, None if it
            cannot be computed)
      height_px: measured band height in px (always returned). If geometry
                 detection has broken down this can be only a few px, in
                 which case mean/clip are not meaningful and are set to
                 None (threshold: below 5px).
    """
    cap_y1 = geo["cap"]["y1"]
    men_y = geo["meniscus"]["y"]
    y0, y1 = cap_y1 + 1, men_y - 1
    height_px = max(0, y1 - y0 + 1)
    x0, x1 = geo["body"]["x0"], geo["body"]["x1"]
    trim = (x1 - x0) * 20 // 100
    xi0, xi1 = x0 + trim, x1 - trim
    if height_px < 5 or xi1 - xi0 < 2:
        return None, None, height_px
    rgb = np.asarray(Image.open(ref_path).convert("RGB"), dtype=np.float64)
    band = rgb[y0:y1 + 1, xi0:xi1]
    mean = [round(float(band[..., ch].mean()), 2) for ch in range(3)]
    clip = round(float((band >= 254).mean()), 4)
    return mean, clip, height_px


def sediment_front(prof: dict, thr: float = 0.08, run: int = 10) -> int | None:
    """Estimate the sedimentation front (row where OD rises) from the 625 nm profile."""
    ods, ys = prof["ods"], prof["ys"]
    count = 0
    for y, od in zip(ys, ods):
        count = count + 1 if od > thr else 0
        if count >= run:
            return ys[ys.index(y) - run + 1]
    return None


# ---------------------------------------------------------------- figure

def draw_figure(run_dir: Path, results: list[dict], geo: dict,
                front_y: int | None, mode: str = "full") -> Path:
    PW, PH = 430, 430
    ML, MT = 60, 96
    STRIP_W = 40
    # the same photo is drawn as a single strip (in white-only mode all
    # channels come from the same photo)
    strips = []
    for r in results:
        if r["photo"] not in [s["photo"] for s in strips]:
            strips.append(r)
    W = ML + len(strips) * (STRIP_W + 8) + 24 + PW + 120
    H = MT + PH + 64
    img = Image.new("RGB", (W, H), (24, 26, 30))
    d = ImageDraw.Draw(img)
    FG, DIM, GRID = (225, 225, 225), (150, 154, 162), (70, 74, 82)
    OD_MAX = 1.0
    starts = [r["profile"]["ys"][0] for r in results if r["profile"]["ys"]]
    y_top = min(starts) if starts else geo["meniscus"]["y"] + 8
    y_bot = geo["liquid"]["bottom_y"] - 22

    if mode == "white":
        d.text((ML, 14), "White transmission: per-channel optical density vs depth",
               fill=FG)
        d.text((ML, 34), "broadband white backlight, R/G/B camera channels, "
                         "sRGB linearized, pedestal subtracted", fill=DIM)
    else:
        d.text((ML, 14), "3-wavelength transmission: optical density vs depth",
               fill=FG)
        d.text((ML, 34), "single-color LED backlight, unclipped secondary channels, "
                         "sRGB linearized, pedestal subtracted", fill=DIM)
    d.text((ML, 54), f"liquid rows {y_top}-{y_bot}, I0 = just below meniscus", fill=DIM)

    sx = ML
    for r in strips:
        crop = np.asarray(Image.open(r["photo"]).convert("RGB"))[
            y_top:y_bot, geo["body"]["x0"]:geo["body"]["x1"]]
        img.paste(Image.fromarray(crop).resize((STRIP_W, PH)), (sx, MT))
        d.text((sx + 2, MT - 18),
               "white" if mode == "white" else r["label"], fill=r["color"])
        sx += STRIP_W + 8

    ax0 = sx + 24
    d.rectangle([ax0, MT, ax0 + PW, MT + PH], outline=GRID)
    for v in (0.2, 0.4, 0.6, 0.8):
        x = ax0 + int(v / OD_MAX * PW)
        d.line([x, MT, x, MT + PH], fill=GRID)
        d.text((x - 10, MT + PH + 8), f"{v:.1f}", fill=DIM)
    d.text((ax0 - 4, MT + PH + 8), "0", fill=DIM)
    d.text((ax0 + PW - 4, MT + PH + 8), "1.0", fill=DIM)
    d.text((ax0 + PW // 2 - 60, MT + PH + 30), "optical density (OD)", fill=FG)
    d.text((ax0 - 46, MT + 2), "top", fill=DIM)
    d.text((ax0 - 46, MT + PH - 12), "bottom", fill=DIM)

    def ypix(y):
        return MT + int((y - y_top) / max(1, y_bot - y_top) * PH)

    if front_y is not None:
        d.line([ax0, ypix(front_y), ax0 + PW, ypix(front_y)], fill=(120, 110, 60))
        d.text((ax0 + 6, ypix(front_y) - 16),
               f"sediment front (~y={front_y})", fill=(210, 190, 110))

    for r in results:
        p = r["profile"]
        pts = [(ax0 + int(np.clip(o, 0, OD_MAX) / OD_MAX * PW), ypix(y))
               for y, o in zip(p["ys"], p["ods"])]
        if len(pts) < 2:
            continue
        d.line(pts, fill=r["color"], width=3)
        ex, ey = pts[-1]
        curve_lbl = (f"ch {r['label']}" if mode == "white"
                     else f"{r['label']} nm")
        d.text((min(ex + 8, ax0 + PW - 56), ey - 6), curve_lbl, fill=r["color"])
        if p["extinct_y"] is not None:
            d.line([ex, ey, ax0 + PW, ey], fill=r["color"], width=1)
            d.text((ax0 + PW + 6, ey - 6), "extinct", fill=r["color"])

    out = run_dir / "spectral.png"
    img.save(out)
    return out


# ---------------------------------------------------------------- top level

def run_spectral(start_intensity: int = 1,
                 restore: tuple[int, int] = (0, 5600),
                 run_dir: Path | None = None,
                 mode: str = "full",
                 locked_geo: dict | None = None,
                 fixed_intensity: bool = False,
                 white_intensity: int | None = None) -> int:
    """Run one capture sequence. A restore brightness of 0 means turning the light off.

    run_dir=None writes to OUT_DIR/spectral_<timestamp> (an unnamed run,
    suitable for aggregation by an external periodic capture loop).
    Passing run_dir saves a named spot measurement to that directory instead.
    mode="white" is a quick measurement that gets R/G/B channel OD from a
    single white shot; since it should not be mixed into aggregated periodic
    output, it is restricted to named saves (run_dir required).
    Passing locked_geo skips auto-detection of the white reference and
    measures using the locked field of view instead (for sample comparison).
    fixed_intensity=True disables automatic adjustment of the monochromatic
    LED intensity and holds it at start_intensity (for cross-sample absolute
    comparison; default is False, i.e. automatic adjustment as before).
    Passing white_intensity also fixes the white-shot intensity (for photo
    comparison / absolute transmittance comparison in white-only mode).
    None means the usual auto-sweep as before. The saved ref_white.jpg is
    always the image captured at exactly this intensity (an invariant).
    Geometry detection is tried on that same image first, and only falls
    back to an auto-intensity sweep dedicated to detection if that fails.
    Values outside 1..100 raise ValueError.
    Concurrent runs are serialized with a lock (so this does not collide
    with a periodic capture loop).
    """
    import hw

    if mode not in ("full", "white"):
        raise ValueError(f"Unknown measurement mode: {mode!r}")
    if mode == "white" and run_dir is None:
        raise ValueError("White-only mode is for named saves only "
                         "(pass --folder/--name)")
    if white_intensity is not None and not (1 <= white_intensity <= 100):
        raise ValueError(f"white_intensity must be in 1..100: "
                         f"{white_intensity}")
    with hw.file_lock(
            LOCK_PATH, LOCK_WAIT_S,
            on_wait=lambda: print("Another measurement is running; waiting for it to finish...")),             run_owner():
        return _run_locked(start_intensity, restore, run_dir, mode, locked_geo,
                           fixed_intensity, white_intensity)


def _run_locked(start_intensity: int, restore: tuple[int, int],
                run_dir: Path | None, mode: str,
                locked_geo: dict | None = None,
                fixed_intensity: bool = False,
                white_intensity: int | None = None) -> int:
    from neewer_light import NeewerLight

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    named = run_dir is not None
    if named:
        run_dir = _unique_dir(run_dir)
    else:
        run_dir = OUT_DIR / f"spectral_{ts}"

    light = NeewerLight()
    # only create the run directory once the camera/light have initialized
    # successfully (so a failed start doesn't leave an empty directory behind)
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Light: {light.name}")
    results = []
    camera_before = camera_after = None
    try:
        # power on under white first, then fix WB and wait for the
        # transition. If it is already at manual 4600, nothing changes (WB
        # is never switched back to auto afterward)
        light.power(True)
        light.set_cct(1, 5600)
        time.sleep(LED_SETTLE_S)
        _fix_white_balance()
        # the server must confirm the stream is not stale before any photo
        # is taken; its read-back settings are this run's camera record
        camera_before = camera_preflight()
        if white_intensity is not None:
            # white-only fixed intensity: the photometry target
            # (ref_white.jpg) is always the image captured at this exact
            # intensity (an invariant). Geometry detection is tried on that
            # image first, and only falls back to an auto-intensity sweep
            # dedicated to detection if that fails (the photometry image
            # itself never changes).
            print(f"Capturing white reference at fixed intensity {white_intensity}%...")
            ref_path = capture_white_image(light, run_dir, white_intensity)
            white_brt = white_intensity
            if locked_geo is not None:
                geo = locked_geo["geometry"]
                print(f"Using locked geometry (skipping detection): meniscus y={geo['meniscus']['y']}, "
                      f"body x={geo['body']['x0']}-{geo['body']['x1']}, "
                      f"liquid bottom y={geo['liquid']['bottom_y']}")
            else:
                geo = _white_geometry_from_fixed_shot(
                    light, run_dir, ref_path, white_intensity)
        elif locked_geo is not None:
            # locked field of view: skip auto-detection and use the saved
            # geometry (still shoot a white image for the record)
            geo = locked_geo["geometry"]
            white_brt = locked_geo.get("white_brightness", 20)
            print(f"Using locked geometry (skipping detection): meniscus y={geo['meniscus']['y']}, "
                  f"body x={geo['body']['x0']}-{geo['body']['x1']}, "
                  f"liquid bottom y={geo['liquid']['bottom_y']}")
            ref_path = capture_white_image(light, run_dir, white_brt)
        else:
            print("Capturing white reference...")
            ref_path, geo, white_brt = capture_white_reference(light, run_dir)
            print(f"  geometry: meniscus y={geo['meniscus']['y']}, "
                  f"body x={geo['body']['x0']}-{geo['body']['x1']}, "
                  f"liquid bottom y={geo['liquid']['bottom_y']}")

        # measure the light's actual effective intensity from the headspace
        # band of ref_white.jpg, always, regardless of mode/white_intensity
        # (used to cross-check illumination after the fact)
        headspace_mean, headspace_clip, headspace_px = white_headspace_stats(
            ref_path, geo)
        if headspace_mean is None:
            print(f"  Headspace band too small (height {headspace_px}px); "
                  "cannot record the measured effective intensity (white_headspace_mean)")

        if mode == "white":
            # get R/G/B channel OD from a single white shot (broadband light
            # so this is not wavelength-resolved; labels are channel names).
            # The white reference's pass/fail check above only looks at a
            # loose geometry-detection criterion (center saturation < 50%),
            # so separately check here for saturation in the liquid-column
            # region used for photometry (saturation would underestimate
            # OD; same inner-60%-column rule as od_profile).
            rgb = np.asarray(Image.open(ref_path).convert("RGB"),
                             dtype=np.float64)
            x0, x1 = geo["body"]["x0"], geo["body"]["x1"]
            trim = (x1 - x0) * 20 // 100
            liq = rgb[geo["meniscus"]["y"] + 8:geo["liquid"]["bottom_y"] - 22,
                      x0 + trim:x1 - trim]
            for ch, color in ((0, (255, 90, 80)), (1, (90, 220, 110)),
                              (2, (110, 160, 255))):
                band = liq[..., ch]
                clip = float((band >= 254).mean())
                mean = float(band.mean())
                warn = None
                if clip > 0.001:
                    warn = (f"Saturated pixels in the photometry region (clip={clip:.1%}, "
                            f"max={band.max():.0f}) -- may underestimate OD")
                elif mean < SEC_MIN:
                    # intensity is not adjusted automatically here (it would
                    # defeat the purpose of a comparison run); just warn if
                    # it is too dim (too bright is caught by the saturation
                    # check above)
                    warn = (f"White intensity {white_brt}% is too dim "
                            f"(channel mean={mean:.1f}) -- the lower OD range may be lost")
                prof = safe_od_profile(ref_path, ch, geo)
                results.append({"name": f"white_{'RGB'[ch]}",
                                "label": "RGB"[ch], "wavelength_nm": None,
                                "channel": "RGB"[ch],
                                "intensity_pct": white_brt,
                                "photo": ref_path, "profile": prof,
                                "color": color, "warning": warn})
        else:
            intensity = start_intensity
            for name, hue, wl, dom_ch, sec_ch, color in COLORS:
                photo, used, warn = capture_color(light, run_dir, name, hue,
                                                  dom_ch, sec_ch, geo, intensity,
                                                  fixed=fixed_intensity)
                prof = safe_od_profile(photo, sec_ch, geo)
                results.append({"name": name, "label": str(wl),
                                "wavelength_nm": wl,
                                "channel": "RGB"[sec_ch], "intensity_pct": used,
                                "photo": photo, "profile": prof, "color": color,
                                "warning": warn})
                intensity = used  # the next color starts from this intensity
        # read the settings back once more after the last photo, so a
        # change during the run is visible in summary.json
        try:
            camera_after = camera_preflight()
        except Exception as e:     # recorded, not fatal: the photos exist
            camera_after = {"error": str(e)}
    finally:
        restored = restore_light(light, restore)
        # WB is kept at manual 4600, never switched back to auto (for color reproducibility)
        print(f"Restore: light {restored}, WB kept at manual {WB_FIXED}")

    return write_run_outputs(
        run_dir, results, geo, mode, ts,
        extra={
            "geometry_locked": locked_geo is not None,
            "fixed_intensity": bool(fixed_intensity),
            "white_intensity_pct": white_brt,
            "white_intensity_fixed": white_intensity is not None,
            # ref_white.jpg's headspace band (a measured probe of effective
            # intensity). mean: R/G/B average, clip: saturated-pixel
            # fraction, px: band height. px<5 suggests broken geometry
            # detection, in which case mean/clip are null (see README.md).
            "white_headspace_mean": headspace_mean,
            "white_headspace_clip": headspace_clip,
            "white_headspace_px": headspace_px,
        },
        camera_before=camera_before, camera_after=camera_after,
        named=named)


def camera_record(before: dict | None, after: dict | None) -> dict:
    """summary.json fields for the camera, from READ-BACK values only.

    camera_settings is what the server read back from the device right
    before the first photo (None = unknown); camera_settings_changed is
    True when the post-run read-back differs.
    """
    rec = {"camera_settings": (before or {}).get("settings"),
           "camera_device": (before or {}).get("device"),
           "camera_matches_reference": (before or {}).get("matches_reference"),
           "camera_mismatches": (before or {}).get("mismatches")}
    if after is not None:
        rec["camera_settings_after"] = after.get("settings")
        if "error" in after:
            rec["camera_settings_after_error"] = after["error"]
        changed = None
        if rec["camera_settings"] is not None and after.get("settings") is not None:
            changed = after["settings"] != rec["camera_settings"]
        rec["camera_settings_changed"] = changed
    return rec


def write_run_outputs(run_dir: Path, results: list[dict], geo: dict,
                      mode: str, ts: str, extra: dict | None = None,
                      camera_before: dict | None = None,
                      camera_after: dict | None = None,
                      named: bool = False) -> int:
    """Write profiles.csv and summary.json, then the figure.

    summary.json is written before plotting, so a failure while drawing
    never loses the record. Empty profiles (dark images, geometry outside
    the image) are allowed: their columns are simply empty. NaN/inf are
    never written (json allow_nan=False enforces it).
    """
    # the sedimentation front is read from the red-side attenuation (full
    # mode: 625 nm, white-only mode: R channel)
    front_y = sediment_front(results[0]["profile"]) if results else None

    # profiles.csv (column names: full mode od_625... / white-only mode
    # od_R...), each followed by od_<label>_cap = the uncorrected profile
    # computed with the cap pedestal alone
    all_ys = [y for r in results
              for y in r["profile"]["ys"] + r["profile"].get("ys_cap", [])]
    lut = {r["label"]: dict(zip(r["profile"]["ys"], r["profile"]["ods"]))
           for r in results}
    lut_cap = {r["label"]: dict(zip(r["profile"].get("ys_cap", []),
                                    r["profile"].get("ods_cap", [])))
               for r in results}
    header = ["y", "depth_px"]
    for r in results:
        header += [f"od_{r['label']}", f"od_{r['label']}_cap"]
    with (run_dir / "profiles.csv").open("w", newline="",
                                        encoding="utf-8") as fp:
        w = csv.writer(fp)
        w.writerow(header)
        if all_ys:
            y_min, y_max = min(all_ys), max(all_ys)
            for y in range(y_min, y_max + 1):
                row = [y, y - y_min]
                for r in results:
                    for table in (lut, lut_cap):
                        od = table[r["label"]].get(y)
                        row.append(f"{od:.4f}" if od is not None and math.isfinite(od) else "")
                w.writerow(row)

    def wl_entry(r):
        p = r["profile"]
        return {
            "nm": r["wavelength_nm"], "label": r["label"],
            "channel": r["channel"],
            "intensity_pct": r["intensity_pct"],
            "pedestal_mode": p.get("pedestal_mode"),
            "pedestal_rule": p.get("pedestal_rule"),
            "pedestal": _finite(p.get("pedestal")),
            "pedestal_cap": _finite(p.get("pedestal_cap")),
            "i0": _finite(p.get("i0")),
            "extinct_y": p.get("extinct_y"),
            "od_last": _finite(round(p["ods"][-1], 3)) if p["ods"] else None,
            "rows": len(p["ys"]),
            "photo": r["photo"].name,
            "warning": r["warning"],
            "analysis_warning": p.get("warning"),
        }

    summary = {
        "captured_at": ts,
        "mode": mode,
        **(extra or {}),
        "geometry": {"meniscus_y": geo["meniscus"]["y"],
                     "liquid_bottom_y": geo["liquid"]["bottom_y"],
                     "body_x0": geo["body"]["x0"], "body_x1": geo["body"]["x1"],
                     "fill_fraction": geo["liquid"].get("fill_fraction_of_body")},
        # the commanded value; the read-back is camera_settings["wb_temp"]
        "white_balance_fixed": WB_FIXED,
        **camera_record(camera_before, camera_after),
        "sediment_front_y": front_y,
        "wavelengths": [wl_entry(r) for r in results],
    }
    if named:
        summary["folder"] = run_dir.parent.name
        summary["run_name"] = run_dir.name
    # encoding is explicit: Windows defaults to cp932, which would mangle
    # non-ASCII warning text and be incompatible with reading it back on macOS.
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8")

    fig = draw_figure(run_dir, results, geo, front_y, mode)
    _print_report(run_dir, results, mode, front_y, fig)
    return 0


def _print_report(run_dir, results, mode, front_y, fig) -> None:
    print(f"\nOutput: {run_dir}")
    for r in results:
        p = r["profile"]
        ext = f", extinct at y={p['extinct_y']}" if p["extinct_y"] else ""
        warn = f" [warning] {r['warning']}" if r["warning"] else ""
        if p.get("warning"):
            warn += f" [analysis] {p['warning']}"
        head = (f"white (ch {r['channel']}" if mode == "white"
                else f"{r['label']}nm (ch {r['channel']}")
        max_od = f"{max(p['ods']):.2f}" if p["ods"] else "n/a"
        print(f"  {head}, intensity {r['intensity_pct']}%): "
              f"max OD {max_od}{ext}{warn}")
    if front_y is not None:
        print(f"  Sediment front: y~={front_y}")
    print(f"  Figure: {fig}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--intensity", type=int, default=1,
                    help="Initial monochromatic LED intensity %% (default: 1)")
    ap.add_argument("--restore", nargs=2, type=int, default=(0, 5600),
                    metavar=("BRT", "KELVIN"),
                    help="Light state to leave after finishing. Brightness 0 means off (default: 0 5600)")
    ap.add_argument("--folder", help="Output folder name (<PHOTOS_DIR>/<folder>/<name>/)")
    ap.add_argument("--name", help="Run name (used together with --folder)")
    ap.add_argument("--mode", choices=("full", "white"), default="full",
                    help="full: white + red/green/blue 3-wavelength measurement (default) / "
                         "white: quick single white-shot measurement (named saves only)")
    ap.add_argument("--calibrate", action="store_true",
                    help="Detect geometry from a white reference and save it as the locked field of view"
                         " (run this once before comparing samples)")
    ap.add_argument("--locked", action="store_true",
                    help="Measure using the saved locked field of view (skip auto-detection)")
    ap.add_argument("--fixed-intensity", action="store_true",
                    help="Fix the monochromatic LED intensity at the initial value"
                         " (for comparison; disables automatic adjustment)")
    ap.add_argument("--white-intensity", type=int, metavar="PCT",
                    help="Fix the white LED intensity %% (for white-only mode comparisons; default is "
                         f"an auto sweep, {WHITE_FIXED_DEFAULT}%% is a reasonable starting point)")
    args = ap.parse_args()
    ensure_dirs()
    if args.calibrate:
        if args.folder or args.locked or args.white_intensity is not None:
            ap.error("--calibrate must be run on its own")
        return calibrate_geometry(tuple(args.restore))
    if bool(args.folder) != bool(args.name):
        ap.error("--folder and --name must both be given")
    if args.mode == "white" and not args.folder:
        ap.error("--mode white requires --folder/--name "
                 "(white-only shots must not land in the periodic-analysis output)")
    if args.white_intensity is not None:
        if args.mode == "full":
            ap.error("In full mode the white shot is only used for geometry detection and does not affect OD. "
                     "Use --fixed-intensity to fix the monochromatic LED intensity instead")
        if not (1 <= args.white_intensity <= 100):
            ap.error(f"--white-intensity must be in 1..100: "
                     f"{args.white_intensity}")
    locked_geo = load_locked_geometry() if args.locked else None
    run_dir = None
    if args.folder:
        try:
            run_dir = (PHOTOS_DIR / validate_component(args.folder)
                       / validate_component(args.name))
        except ValueError as e:
            ap.error(str(e))
    return run_spectral(args.intensity, tuple(args.restore), run_dir,
                        args.mode, locked_geo,
                        fixed_intensity=args.fixed_intensity,
                        white_intensity=args.white_intensity)


if __name__ == "__main__":
    sys.exit(main())
