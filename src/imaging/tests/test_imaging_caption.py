"""Item 1: the draft caption comes from summary.json and measured clip
fractions, never from hard-coded conditions (no hardware)."""
import numpy as np
import pytest

import make_panel as mp
import paths
from helpers_imaging import H, W, save_rows

EXAMPLE = paths.EXAMPLES_DIR / "zif8_vial_5s"


def test_row_clip_fractions_are_per_pixel(tmp_path):
    # half the inner columns saturated: row mean ~ 190 (< 250), so the old
    # row-mean test would say "unclipped"; per-pixel says 50 %
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[:, :, 1] = 130
    img[:, 170:200, 1] = 255                     # body 150..250 -> inner 170..230
    from PIL import Image
    p = tmp_path / "half.png"
    Image.fromarray(img).save(p)
    fr = mp.row_clip_fractions(p, 1, 150, 250)
    assert fr.shape == (H,)
    assert fr[300] == pytest.approx(0.5)
    s = mp.clip_summary(fr, 250, 650)
    assert s["pixel_frac"] == pytest.approx(0.5)
    assert s["rows_any"] == 1.0 and s["rows_majority"] == 0.0


def test_clip_summary_empty_range():
    assert mp.clip_summary(np.zeros(10), 20, 30)["pixel_frac"] is None


def _clip(pix):
    return {"pixel_frac": pix, "rows_any": 1.0 if pix else 0.0,
            "rows_majority": 1.0 if pix and pix > 0.5 else 0.0, "n_rows": 10}


FULL_SUMMARY = {
    "captured_at": "20990101_000000", "white_intensity_pct": 7,
    "camera_settings": {"exposure": 33, "gain": 4, "focus": 99,
                        "auto_exposure": False, "auto_wb": False,
                        "wb_temp": 4700},
    "camera_device": {"name": "Some Cam"},
    "wavelengths": [
        {"label": "625", "channel": "G", "intensity_pct": 3},
        {"label": "525", "channel": "B", "intensity_pct": 5, "warning": "dom hot"},
        {"label": "465", "channel": "G", "intensity_pct": 9}],
    "geometry": {"meniscus_y": 1, "liquid_bottom_y": 2, "body_x0": 3, "body_x1": 4},
}


def test_caption_uses_recorded_values_not_constants():
    clip = {"white": _clip(0.2), "625": _clip(0.3), "525": _clip(0.0),
            "465": _clip(0.0)}
    c = mp.build_caption(FULL_SUMMARY, "run", "dir", clip, with_white=False)
    for text in ("625 nm (HSI, 3%)", "525 nm (HSI, 5%)", "465 nm (HSI, 9%)",
                 "intensity 7%", "exposure 33", "gain 4", "focus 99",
                 "manual 4700 K", "Some Cam", "525 nm: dom hot"):
        assert text in c, text
    # 625 is the clipped one here; the hard-coded claim must be gone
    assert "625 nm (30.0% of liquid-region pixels" in c
    assert "525 nm and 465 nm have no clipped pixel" in c
    assert "1% intensity" not in c and "exposure 20" not in c
    assert "625 nm and 525 nm remain unclipped" not in c


def test_caption_says_unknown_when_not_recorded():
    summary = {"wavelengths": [{"label": "625", "channel": "G"}]}
    c = mp.build_caption(summary, "run", "dir", {}, with_white=True)
    assert "exposure unknown" in c and "gain unknown" in c
    assert "625 nm (HSI, unknown)" in c and "intensity unknown" in c
    assert "camera model unknown" in c and "white balance unknown" in c
    assert "Clipping unknown for 625 nm, 525 nm and 465 nm" in c
    assert "captured unknown" in c


def test_make_panel_on_example_run(tmp_path, capsys):
    mp.main([str(EXAMPLE), "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Clipped pixels" in out
    cap = (tmp_path / "zif8_vial_5s_caption.md").read_text(encoding="utf-8")
    # the example predates the camera read-back: conditions are unknown
    assert "exposure unknown" in cap
    assert "exposure 20" not in cap
    # measured from the photos: 465 nm clipped, 625/525 not
    assert "sensor-clipped for 465 nm" in cap
    assert "625 nm and 525 nm have no clipped pixel" in cap
    assert (tmp_path / "zif8_vial_5s_panel.png").is_file()
