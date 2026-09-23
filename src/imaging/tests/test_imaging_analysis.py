"""Item 5: od_profile / output robustness, capture file names (no hardware)."""
import csv
import json
import math
from pathlib import Path

import numpy as np
import pytest

import acquire
import camera_server
import paths
from helpers_imaging import H, FakeLight, geo, linear_to_srgb, save_rows

EXAMPLE = paths.EXAMPLES_DIR / "zif8_vial_5s"


def _strict_json(path: Path):
    def bad(c):
        raise ValueError(f"non-finite constant {c} in {path}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=bad)


def test_dark_image_gives_empty_profile_without_nan(tmp_path):
    img = save_rows(tmp_path / "dark.jpg", np.zeros(H))
    prof = acquire.od_profile(img, 1, geo())
    assert prof["ys"] == [] and prof["ods"] == []
    assert prof["ys_cap"] == []
    assert prof["warning"] and "No transmitted signal" in prof["warning"]
    for k in ("i0", "pedestal", "i0_cap"):
        assert prof[k] is None or math.isfinite(prof[k])


@pytest.mark.parametrize("bad", [
    {"meniscus": {"y": 900}},
    {"body": {"x0": 350, "x1": 500, "width_px": 150}},
    {"liquid": {"bottom_y": 280, "fill_fraction_of_body": 0.1}},
    {"cap": {"y0": -5, "y1": 150}},
])
def test_geometry_outside_image_is_rejected(tmp_path, bad):
    img = save_rows(tmp_path / "grey.jpg", np.full(H, 120))
    g = geo()
    g.update(bad)
    with pytest.raises(ValueError, match="Geometry does not fit"):
        acquire.od_profile(img, 1, g)
    prof = acquire.safe_od_profile(img, 1, g)       # the run keeps going
    assert prof["ys"] == [] and "Geometry does not fit" in prof["warning"]


def test_min_then_rise_floor_warns_and_keeps_cap_profile(tmp_path):
    g = geo()
    y = np.arange(H)
    lin = np.full(H, 0.0015)                        # dark background / cap
    top, low = 258, 450
    liquid = (y >= g["meniscus"]["y"]) & (y < g["liquid"]["bottom_y"])
    decay = 0.6 * np.exp(-(y - top) / 40.0)
    rise = decay[low] + (y - low) * 0.002           # stray light rising again
    lin[liquid] = np.where(y[liquid] < low, decay[liquid], rise[liquid])
    img = save_rows(tmp_path / "rise.jpg", linear_to_srgb(lin))
    prof = acquire.od_profile(img, 1, g)
    assert prof["pedestal_mode"] == "floor"
    assert prof["pedestal_rule"] == "min_then_rise"
    assert "minimum-then-rise" in prof["warning"]
    # the uncorrected cap-pedestal profile is kept, and differs
    assert prof["ys_cap"] and prof["ods_cap"] != prof["ods"]
    assert all(math.isfinite(v) for v in prof["ods"] + prof["ods_cap"])


def test_example_run_still_reproduces_published_profiles():
    """Refactoring must not change the numbers of the example run."""
    from geometry import extract_features
    s = json.loads((EXAMPLE / "summary.json").read_text(encoding="utf-8"))
    feat = extract_features(EXAMPLE / "ref_white.jpg")
    gs = s["geometry"]
    feat["meniscus"]["y"] = gs["meniscus_y"]
    feat["liquid"]["bottom_y"] = gs["liquid_bottom_y"]
    feat["body"]["x0"], feat["body"]["x1"] = gs["body_x0"], gs["body_x1"]
    with open(EXAMPLE / "profiles.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for label, photo, ch in (("625", "shot_red.jpg", 1),
                             ("525", "shot_green.jpg", 2),
                             ("465", "shot_blue.jpg", 1)):
        prof = acquire.od_profile(EXAMPLE / photo, ch, feat)
        col = "od_" + label
        ref = {int(r["y"]): float(r[col]) for r in rows if r[col]}
        got = dict(zip(prof["ys"], prof["ods"]))
        assert set(got) == set(ref)
        assert max(abs(got[y] - v) for y, v in ref.items()) < 1e-4
        assert prof["pedestal_rule"] == "cap" and prof["warning"] is None
        assert prof["ods_cap"] == prof["ods"]


def _one_result(photo, prof, warning=None):
    return [{"name": "red", "label": "625", "wavelength_nm": 625,
             "channel": "G", "intensity_pct": 100, "photo": photo,
             "profile": prof, "color": (255, 0, 0), "warning": warning}]


def test_write_outputs_with_empty_profiles_writes_summary(tmp_path):
    g = geo()
    photo = save_rows(tmp_path / "shot_red.jpg", np.zeros(H))
    prof = acquire.od_profile(photo, 1, g)
    before = {"settings": {"exposure": 20}, "device": {"name": "cam"},
              "matches_reference": True, "mismatches": []}
    assert acquire.write_run_outputs(tmp_path, _one_result(photo, prof, "too dim"),
                                     g, "full", "ts",
                                     camera_before=before,
                                     camera_after=before) == 0
    s = _strict_json(tmp_path / "summary.json")
    assert s["wavelengths"][0]["rows"] == 0
    assert s["wavelengths"][0]["od_last"] is None
    assert s["camera_settings"] == {"exposure": 20}
    assert s["camera_settings_changed"] is False
    lines = (tmp_path / "profiles.csv").read_text(encoding="utf-8").splitlines()
    assert lines == ["y,depth_px,od_625,od_625_cap"]
    assert (tmp_path / "spectral.png").is_file()


def test_summary_written_before_plotting(tmp_path, monkeypatch):
    g = geo()
    photo = save_rows(tmp_path / "shot_red.jpg", np.zeros(H))
    results = _one_result(photo, acquire.od_profile(photo, 1, g))

    def boom(*a, **k):
        raise RuntimeError("plot failed")
    monkeypatch.setattr(acquire, "draw_figure", boom)
    with pytest.raises(RuntimeError, match="plot failed"):
        acquire.write_run_outputs(tmp_path, results, g, "full", "ts")
    assert (tmp_path / "summary.json").is_file()
    assert (tmp_path / "profiles.csv").is_file()


def test_capture_names_unique_and_exclusive(tmp_path, monkeypatch):
    names = {camera_server.capture_name() for _ in range(200)}
    assert len(names) == 200
    assert all(n.startswith("sample_") and n.endswith(".jpg") for n in names)
    p1 = camera_server.save_capture(b"a", tmp_path)
    # a colliding name is never overwritten: exclusive create retries
    taken = iter([p1.name, "sample_fresh.jpg"])
    monkeypatch.setattr(camera_server, "capture_name", lambda: next(taken))
    p2 = camera_server.save_capture(b"b", tmp_path)
    assert p2.name == "sample_fresh.jpg"
    assert p1.read_bytes() == b"a" and p2.read_bytes() == b"b"


# ------------------------------------------------ full run with fakes

HEALTH_OK = {"ok": True, "frame": {"fresh": True}, "ffmpeg": {"alive": True},
             "stream_settings_stale": False,
             "camera": {"connected": True, "matches_reference": False,
                        "mismatches": [{"key": "exposure", "expected": 20,
                                        "actual": 156}],
                        "settings": {"auto_exposure": False, "exposure": 156,
                                     "gain": 0, "auto_focus": False,
                                     "focus": 180, "auto_wb": False,
                                     "wb_temp": 4600},
                        "device": {"name": "HD Pro Webcam C920", "index": 1}}}


@pytest.fixture
def fake_rig(monkeypatch):
    """Fake light, camera server and capture; every photo is uniformly dark."""
    import capture
    import neewer_light
    lights = []

    def make_light(*a, **k):
        lights.append(FakeLight())
        return lights[-1]
    monkeypatch.setattr(neewer_light, "NeewerLight", make_light)
    monkeypatch.setattr(acquire.time, "sleep", lambda s: None)
    state = {"health": json.loads(json.dumps(HEALTH_OK)), "tokens": [],
             "shots": 0, "lights": lights,
             "controls": {"autoWhiteBalance": False, "whiteBalance": 4600}}

    def fake_capture():
        state["shots"] += 1
        paths.PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        name = "sample_%d.jpg" % state["shots"]
        return save_rows(paths.PHOTOS_DIR / name, np.zeros(H))

    def fake_health():
        state["tokens"].append((capture._run_token,
                                paths.RUN_OWNER_PATH.read_text(encoding="utf-8")))
        return state["health"]
    monkeypatch.setattr(capture, "capture_photo", fake_capture)
    monkeypatch.setattr(capture, "get_health", fake_health)
    monkeypatch.setattr(capture, "get_control", lambda n: state["controls"][n])
    monkeypatch.setattr(capture, "set_control", lambda n, v: v)
    return state


LOCKED = {"geometry": geo(), "white_brightness": 20}


def test_full_run_on_dark_images_records_read_back_camera(fake_rig, tmp_path):
    run_dir = tmp_path / "run"
    assert acquire.run_spectral(run_dir=run_dir, locked_geo=LOCKED) == 0
    s = _strict_json(run_dir / "summary.json")
    # read back from the server, not the reference constants
    assert s["camera_settings"] == HEALTH_OK["camera"]["settings"]
    assert s["camera_matches_reference"] is False and s["camera_mismatches"]
    assert s["camera_settings_changed"] is False
    assert all(w["rows"] == 0 and w["analysis_warning"] for w in s["wavelengths"])
    # the run's own token was registered and attached; removed afterwards
    tok, on_disk = fake_rig["tokens"][0]
    assert tok and tok == on_disk
    assert not paths.RUN_OWNER_PATH.exists()
    light = fake_rig["lights"][0]
    assert light.calls[-2:] == [("power", False), ("close",)]


def test_stale_stream_aborts_before_any_photo(fake_rig, tmp_path):
    fake_rig["health"]["stream_settings_stale"] = True
    with pytest.raises(RuntimeError, match="stale"):
        acquire.run_spectral(run_dir=tmp_path / "run", locked_geo=LOCKED)
    assert fake_rig["shots"] == 0
    assert fake_rig["lights"][0].calls[-2:] == [("power", False), ("close",)]


def test_not_fresh_stream_aborts(fake_rig, tmp_path):
    fake_rig["health"]["frame"]["fresh"] = False
    with pytest.raises(RuntimeError, match="no fresh frame"):
        acquire.run_spectral(run_dir=tmp_path / "run", locked_geo=LOCKED)
    assert fake_rig["shots"] == 0


def test_wb_read_back_mismatch_aborts(fake_rig, tmp_path):
    fake_rig["controls"]["whiteBalance"] = 5000     # write "succeeded", did not stick
    with pytest.raises(RuntimeError, match="White balance did not stick"):
        acquire.run_spectral(run_dir=tmp_path / "run", locked_geo=LOCKED)
    assert fake_rig["shots"] == 0
