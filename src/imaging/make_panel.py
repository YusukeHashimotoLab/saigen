#!/usr/bin/env python3
"""Publication panel figure (white/625/525/465 nm crops + depth transmittance trend).

Reads `<run>/{ref_white.jpg,shot_red.jpg,shot_green.jpg,shot_blue.jpg,
summary.json,profiles.csv}`, crops the same rectangle (cap bottom to vial
bottom) out of all four photos and lays them out side by side, then draws
the depth-resolved transmittance (or OD) trend on the right using the same
vertical scale (shared image row y) as the photos. Horizontal lines for the
sedimentation front / meniscus / liquid bottom are drawn at the same y in
every panel so you can check that the photo boundaries and curve inflections
line up.

The 625/525/465 curves use the values from `profiles.csv` as-is (so they
match the published analysis exactly, without recomputation). White is not
in profiles.csv, so it is computed by importing `acquire.od_profile()`
(confirmed side-effect free on import: it does not connect over BLE or
capture a photo unless explicitly called).

The white G curve is not drawn in (e) by default: the dip just below the
meniscus contaminates I0 for a few rows, and in the example run the
supernatant is sRGB-saturated there, so it cannot be treated as a real
measurement. `--with-white` draws it anyway, as a plain solid line like the
other curves (clipped rows are not marked in the plot; the clip fractions
are reported in the stdout table and in the caption instead). The clip
table is always printed to stdout regardless of this flag.

Clipping is measured per pixel: for every image row, the fraction of the
inner-60% body pixels whose secondary-channel sRGB value is >= SAT_THR.
The draft caption is built from summary.json (illumination intensities,
the camera settings read back at capture time, the camera device) and from
these clip fractions; anything summary.json does not record is written as
"unknown" rather than filled in with the reference values.

`geometry.py` / `acquire.py` are only imported, never modified.
This script never writes into the run directory or any data directory; all
output goes to --out.

Usage:
    python3 make_panel.py <run_dir>
    python3 make_panel.py <run_dir> --y od
    python3 make_panel.py <run_dir> --with-white
    python3 make_panel.py <run_dir> --mm-per-px 0.083
    python3 make_panel.py <run_dir> --out ./panels

Output (under --out, default ./panels):
    <run_name>_panel.png (300 dpi)
    <run_name>_panel.pdf
    <run_name>_caption.md
"""
import argparse
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from geometry import BG, extract_features          # noqa: E402  (import only)
from acquire import od_profile                      # noqa: E402  (import only, no side effects)
from paths import resolve_input                     # noqa: E402

DEFAULT_OUT_DIR = Path("./panels")

# ---------------------------------------------------------------- style parameters
COLORS = {"white": "#555555", "625": "#d62728", "525": "#2ca02c", "465": "#1f77b4"}
SAT_THR = 250.0          # saturation threshold (raw sRGB, same column-trim rule as od_profile)
EDGE_DARK_THR = BG       # threshold for verifying the crop rectangle's left/right edges miss the side panels
BOT_MARGIN_PX = 70       # bottom crop margin (px, measured downward from liquid_bottom_y)

FIG_WIDTH_MM = 180.0
LEFT_MARGIN_IN = 0.06
RIGHT_MARGIN_IN = 0.12   # the y-axis was moved to the left spine, so only a thin margin is needed on the right
TOP_MARGIN_IN = 0.06     # no title row, so only a thin top margin is needed
BOTTOM_MARGIN_IN = 0.38  # just enough for the right panel's x ticks/axis title (no footnote)
COL_GAP_IN = 0.07        # gap between the (a)-(b)-(c)-(d) photos
# gap between (d) and (e): panel (e)'s y-axis (ticks + axis title) sits here,
# so it needs more room than the gaps between photos. The tick labels were
# moved from the right spine to the left spine, which needs about 0.3in of
# display width here; at 0.07in the tick text would overlap photo (d), so
# only this one gap was widened (the others are unchanged).
DE_GAP_IN = 0.5
WIDTH_RATIOS = [1, 1, 1, 1, 1.3]

CHAN_IDX = {"R": 0, "G": 1, "B": 2}


# ---------------------------------------------------------------- helpers

def row_clip_fractions(path: Path, channel_idx: int, x0: int, x1: int,
                       thr: float = SAT_THR) -> np.ndarray:
    """Per-row fraction of clipped pixels (sRGB >= thr) in one channel.

    Uses the same column-trim rule as od_profile (inner 60% of the body's
    columns). One value per image row, in 0..1.
    """
    trim = (x1 - x0) * 20 // 100
    xi0, xi1 = x0 + trim, x1 - trim
    img = np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)
    return (img[:, xi0:xi1, channel_idx] >= thr).mean(axis=1)


def clip_summary(fracs: np.ndarray, y0: int, y1: int) -> dict:
    """Summarise per-row clip fractions over rows y0..y1 (inclusive).

    pixel_frac: fraction of all pixels in those rows that are clipped;
    rows_any: fraction of rows with at least one clipped pixel;
    rows_majority: fraction of rows with more than half their pixels clipped.
    """
    rows = np.asarray(fracs[y0:y1 + 1], dtype=np.float64)
    if rows.size == 0:
        return {"pixel_frac": None, "rows_any": None, "rows_majority": None,
                "n_rows": 0}
    return {"pixel_frac": float(rows.mean()),
            "rows_any": float((rows > 0).mean()),
            "rows_majority": float((rows > 0.5).mean()),
            "n_rows": int(rows.size)}


def _fmt(v, unit: str = "") -> str:
    return "unknown" if v is None else f"{v}{unit}"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _pct(v) -> str:
    return "unknown" if v is None else f"{v:.1%}"


def build_caption(summary: dict, run_name: str, run_dir, clip: dict,
                  with_white: bool) -> str:
    """Draft figure caption built only from summary.json and measured clip
    fractions (clip: label -> clip_summary(); labels white/625/525/465).

    Values summary.json does not record are written as "unknown".
    """
    wl = {str(w.get("label")): w for w in summary.get("wavelengths", [])}
    cam = summary.get("camera_settings") or {}
    dev = (summary.get("camera_device") or {}).get("name")

    def inten(label):
        return _fmt((wl.get(label) or {}).get("intensity_pct"), "%")

    def chan(label):
        return (wl.get(label) or {}).get("channel") or "unknown"

    white_int = _fmt(summary.get("white_intensity_pct"), "%")
    illum = (f"white (CCT, intensity {white_int}), 625 nm (HSI, {inten('625')}), "
             f"525 nm (HSI, {inten('525')}) and 465 nm (HSI, {inten('465')})")

    if cam.get("wb_temp") is not None and cam.get("auto_wb") is False:
        wb = f"white balance read back as manual {cam['wb_temp']} K"
    elif cam.get("wb_temp") is not None:
        wb = (f"white balance {cam['wb_temp']} K with auto white balance "
              f"{_fmt(cam.get('auto_wb'))}")
    elif summary.get("white_balance_fixed") is not None:
        wb = (f"white balance commanded to manual "
              f"{summary['white_balance_fixed']} K (read-back not recorded)")
    else:
        wb = "white balance unknown"
    cam_desc = (f"{dev or 'camera model unknown (not recorded in summary.json)'}; "
                f"exposure {_fmt(cam.get('exposure'))}, gain {_fmt(cam.get('gain'))}, "
                f"focus {_fmt(cam.get('focus'))}, auto exposure "
                f"{_fmt(cam.get('auto_exposure'))}, {wb}")
    if not cam:
        cam_desc += (" (camera settings were not recorded for this run, so "
                     "the conditions above are unknown)")

    clipped, unclipped, unknown = [], [], []
    for label in ("625", "525", "465"):
        c = clip.get(label) or {}
        if c.get("pixel_frac") is None:
            unknown.append(f"{label} nm")
        elif c["pixel_frac"] == 0:
            unclipped.append(f"{label} nm")
        else:
            clipped.append(
                f"{label} nm ({_pct(c['pixel_frac'])} of liquid-region pixels; "
                f"{_pct(c['rows_majority'])} of rows more than half clipped)")
    parts = []
    if clipped:
        parts.append("In this run the secondary channel is sensor-clipped for "
                     + _join(clipped) + "; over clipped rows the plotted "
                     "value is limited by the sensor, not a true "
                     "transmittance measurement.")
    if unclipped:
        parts.append(_join(unclipped) + " "
                     + ("has" if len(unclipped) == 1 else "have")
                     + " no clipped pixel in the liquid region.")
    if unknown:
        parts.append("Clipping unknown for " + _join(unknown) + ".")
    clip_clause = " ".join(parts)

    cw = clip.get("white") or {}
    wfrac = _pct(cw.get("pixel_frac"))
    if with_white:
        white_clause = (f"the white curve uses the camera's G channel "
                        f"({wfrac} of its liquid-region pixels clipped). ")
    else:
        white_clause = (f"the white photograph (a) is shown for visual "
                        f"reference only ({wfrac} of its G-channel "
                        f"liquid-region pixels clipped); no white "
                        f"transmittance curve is plotted. ")

    warns = [f"{label} nm: {w['warning']}" for label, w in wl.items()
             if w.get("warning")]
    warn_clause = ("Capture warnings recorded in summary.json: "
                   + "; ".join(warns) + ". ") if warns else ""

    g = summary.get("geometry") or {}
    return f"""# Figure caption (draft) — {run_name}

**Depth-resolved transmittance panel.** Backlit photographs of the sample
vial ({run_name}, captured {summary.get('captured_at') or 'unknown'}) under
{illum} illumination (wavelengths nominal, unmeasured, ±10-15 nm), imaged
with {cam_desc}. Left to right: (a-d) sample region (cap bottom to vial
bottom) cropped to a common rectangle across all four exposures, shown
without panel labels; (e) optical density / transmittance versus image row,
sharing the same vertical scale (pixel row) as (a-d) so that visual features
line up horizontally with the photographs. In (e), the red/green/blue curves
correspond to the 625/525/465 nm (nominal) illumination channels
respectively, plotted as solid lines throughout their full depth range. Each
monochromatic channel is measured on its non-dominant ("secondary")
camera channel (625->{chan('625')}, 525->{chan('525')}, 465->{chan('465')});
{white_clause}Values are sRGB-linearized, pedestal-subtracted, with I0 taken
just below the meniscus. {clip_clause} {warn_clause}Clip fractions: secondary
channel sRGB >= {SAT_THR:.0f}, inner 60% of body columns, liquid rows
meniscus_y..liquid_bottom_y.

Geometry: meniscus_y={g.get('meniscus_y')}, liquid_bottom_y={g.get('liquid_bottom_y')},
body_x0/x1={g.get('body_x0')}/{g.get('body_x1')}, sediment_front_y={summary.get('sediment_front_y')}
(from {run_dir}/summary.json).
"""


def srgb_row_means(path: Path, channel_idx: int, x0: int, x1: int) -> np.ndarray:
    """Per-row mean sRGB value (still non-linear). Uses the same column-trim
    rule as od_profile (inner 60% of the body's columns)."""
    trim = (x1 - x0) * 20 // 100
    xi0, xi1 = x0 + trim, x1 - trim
    img = np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)
    return img[:, xi0:xi1, channel_idx].mean(axis=1)


def load_csv_channel(csv_path: Path, col: str) -> tuple[list[int], list[float]]:
    ys, ods = [], []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = row[col]
            if v == "":
                continue
            ys.append(int(row["y"]))
            ods.append(float(v))
    return ys, ods


def plot_curve(ax, ys: list[int], vals: list[float], color: str,
              label: str) -> None:
    """Draw a curve as a solid line over its full range. Sensor-clipped
    regions are not dashed or otherwise marked (drawing is unaffected by
    the clip check; that check feeds the stdout table and the caption)."""
    if not ys:
        return
    ax.plot(vals, ys, color=color, linestyle="solid", linewidth=1.3, label=label)


def make_depth_ticks(y_top: int, y_bot: int, men_y: int,
                     mm_per_px: float | None = None):
    """Return depth-axis tick positions (image row y) and their labels.

    With mm_per_px given, ticks are placed at round 5mm steps; otherwise at
    50px steps.
    """
    if mm_per_px:
        step = 5.0
        lo = (min(y_top, y_bot) - men_y) * mm_per_px
        hi = (max(y_top, y_bot) - men_y) * mm_per_px
        start = step * math.floor(lo / step)
        end = step * math.ceil(hi / step)
        n = int(round((end - start) / step))
        depths = [start + i * step for i in range(n + 1)]
        rows = [men_y + d / mm_per_px for d in depths]
        labels = [f"{d:g}" for d in depths]
    else:
        step = 50
        lo = min(y_top, y_bot) - men_y
        hi = max(y_top, y_bot) - men_y
        start = step * (lo // step)
        end = step * (-(-hi // step))  # ceil
        depths = list(range(start, end + step, step))
        rows = [men_y + d for d in depths]
        labels = [str(d) for d in depths]
    return rows, labels


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path, help="e.g. photos/expA/vial1")
    ap.add_argument("--y", dest="xquantity", choices=["transmittance", "od"],
                    default="transmittance",
                    help="Right-panel x-axis: transmittance=T=10^-OD (default) / od=OD")
    ap.add_argument("--with-white", action="store_true",
                    help="Also draw the white G curve in (e) (not drawn by default; "
                         "see the module docstring for why)")
    ap.add_argument("--mm-per-px", type=float, default=None,
                    help="If given, show (e)'s y-axis ticks in mm (5mm steps) instead of px")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR,
                    help=f"Output directory (default: {DEFAULT_OUT_DIR})")
    args = ap.parse_args(argv)

    # resolved relative to the current directory first, then to the module
    # directory, so `examples/zif8_vial_5s` works from src/imaging and from
    # the repository root alike (see paths.resolve_input)
    run_dir: Path = resolve_input(args.run_dir)
    if not run_dir.is_dir():
        sys.exit(f"Run directory not found: {args.run_dir}")
    run_dir = run_dir.resolve()

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    run_name = run_dir.name
    if not run_name or run_name == ".":
        run_name = summary.get("run_name") or run_name

    geo_s = summary["geometry"]
    men_y = int(geo_s["meniscus_y"])
    bot_y = int(geo_s["liquid_bottom_y"])
    body_x0 = int(geo_s["body_x0"])
    body_x1 = int(geo_s["body_x1"])
    front_y = summary.get("sediment_front_y")

    ref_white = run_dir / "ref_white.jpg"
    feat = extract_features(ref_white)
    cap_y1 = feat["cap"]["y1"]
    vial_x0, vial_x1 = feat["vial"]["x0"], feat["vial"]["x1"]

    # ---- crop rectangle (shared by all 4 photos) ----
    x0c, x1c = vial_x0 - 6, vial_x1 + 6
    img_h = Image.open(ref_white).height
    y0c, y1c = cap_y1 + 1, min(bot_y + BOT_MARGIN_PX, img_h - 1)

    # verify the crop rectangle's left/right edges do not reach the side panels
    for name in ("ref_white.jpg", "shot_red.jpg", "shot_green.jpg", "shot_blue.jpg"):
        img = np.asarray(Image.open(run_dir / name).convert("RGB"), dtype=np.float64)
        lum = img[men_y:bot_y + 1, :, :] @ np.array([0.299, 0.587, 0.114])
        left_edge = float(lum[:, x0c].mean())
        right_edge = float(lum[:, x1c].mean())
        assert left_edge < EDGE_DARK_THR and right_edge < EDGE_DARK_THR, (
            f"{name}: the crop rectangle's left/right edge may be reaching the side panel "
            f"(left={left_edge:.1f}, right={right_edge:.1f}, thr={EDGE_DARK_THR})")
    print(f"Crop rectangle edge darkness check: OK (x0={x0c}, x1={x1c}, all < {EDGE_DARK_THR})")

    # ---- photo/channel per wavelength (read dynamically from summary.json) ----
    wl_info = {str(w["label"]): w for w in summary["wavelengths"]}
    color_specs = []  # (label, photo_path, channel_idx, csv_col)
    for label in ("625", "525", "465"):
        w = wl_info[label]
        color_specs.append((label, run_dir / w["photo"], CHAN_IDX[w["channel"]],
                            f"od_{label}"))

    # ---- white geometry (overridden with summary.json values; only meniscus/liquid.bottom/body per spec) ----
    geo_white = json.loads(json.dumps(feat))  # deep copy
    geo_white["meniscus"]["y"] = men_y
    geo_white["liquid"]["bottom_y"] = bot_y
    geo_white["body"]["x0"] = body_x0
    geo_white["body"]["x1"] = body_x1
    prof_white = od_profile(ref_white, sec_ch=CHAN_IDX["G"], geo=geo_white)

    # ---- per-pixel clipping per row (sRGB >= SAT_THR, same column trim as od_profile) ----
    row_means = {}         # label -> float array (all rows, for the stdout table)
    clip = {}              # label -> clip_summary() over the liquid rows
    sat_specs = [("white", ref_white, CHAN_IDX["G"])] + [
        (label, photo, ch) for label, photo, ch, _ in color_specs]
    for label, photo, ch in sat_specs:
        row_means[label] = srgb_row_means(photo, ch, body_x0, body_x1)
        clip[label] = clip_summary(
            row_clip_fractions(photo, ch, body_x0, body_x1), men_y, bot_y)

    # ---- stdout: clip table (liquid rows = meniscus_y..liquid_bottom_y) ----
    # the white row is always printed regardless of --with-white.
    print("\nClipped pixels (liquid rows y=%d..%d, sRGB >= %.0f, inner 60%% of body columns):" %
          (men_y, bot_y, SAT_THR))
    print(f"{'channel':<14}{'photo':<16}{'pix_frac':>10}{'rows>0':>8}{'rows>50%':>10}"
          f"{'mean min':>10}{'mean max':>10}")
    label_to_ch_name = {"white": "white (G)"}
    label_to_ch_name.update({label: f"{label} ({wl_info[label]['channel']})"
                             for label, _, _, _ in color_specs})
    label_to_photo = {"white": "ref_white.jpg"}
    label_to_photo.update({label: p.name for label, p, _, _ in color_specs})
    for label in ("white", "625", "525", "465"):
        liquid = row_means[label][men_y:bot_y + 1]
        c = clip[label]
        print(f"{label_to_ch_name[label]:<14}{label_to_photo[label]:<16}"
              f"{c['pixel_frac']:>10.1%}{c['rows_any']:>8.1%}{c['rows_majority']:>10.1%}"
              f"{liquid.min():>10.1f}{liquid.max():>10.1f}")
    if not args.with_white:
        print("(white G is not plotted in (e) because it is a contaminated measurement; "
              "use --with-white to draw it anyway)")

    # ---- physical figure size (height derived automatically from the photo aspect ratio) ----
    crop_w = x1c - x0c + 1
    crop_h = y1c - y0c + 1
    fig_width_in = FIG_WIDTH_MM / 25.4
    inner_width_in = fig_width_in - LEFT_MARGIN_IN - RIGHT_MARGIN_IN
    gaps_in = [COL_GAP_IN, COL_GAP_IN, COL_GAP_IN, DE_GAP_IN]
    unit_in = (inner_width_in - sum(gaps_in)) / sum(WIDTH_RATIOS)
    photo_w_in = unit_in
    curve_w_in = unit_in * WIDTH_RATIOS[-1]
    photo_h_in = photo_w_in * (crop_h / crop_w)
    fig_height_in = TOP_MARGIN_IN + photo_h_in + BOTTOM_MARGIN_IN

    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["DejaVu Sans"],
        "font.size": 8,
        "axes.labelsize": 7,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.fontsize": 6.5,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
    })

    fig = plt.figure(figsize=(fig_width_in, fig_height_in))

    col_widths_in = [unit_in, unit_in, unit_in, unit_in, curve_w_in]
    axes = []
    x_cursor_in = LEFT_MARGIN_IN
    bottom_frac = BOTTOM_MARGIN_IN / fig_height_in
    height_frac = photo_h_in / fig_height_in
    for i, w_in in enumerate(col_widths_in):
        left_frac = x_cursor_in / fig_width_in
        width_frac = w_in / fig_width_in
        ax = fig.add_axes([left_frac, bottom_frac, width_frac, height_frac])
        axes.append(ax)
        gap = gaps_in[i] if i < len(gaps_in) else 0.0
        x_cursor_in += w_in + gap

    ax_white, ax_625, ax_525, ax_465, ax_curve = axes
    photo_axes = [(ax_white, ref_white),
                  (ax_625, run_dir / wl_info["625"]["photo"]),
                  (ax_525, run_dir / wl_info["525"]["photo"]),
                  (ax_465, run_dir / wl_info["465"]["photo"])]

    for ax, photo in photo_axes:
        crop = np.asarray(Image.open(photo).convert("RGB"))[y0c:y1c + 1, x0c:x1c + 1]
        ax.imshow(crop, extent=[x0c, x1c, y1c, y0c])
        ax.set_xlim(x0c, x1c)
        ax.set_ylim(y1c, y0c)
        # aspect="auto" so the axes box geometry (sized in inches above to
        # match the crop's aspect ratio) governs the rendered geometry,
        # rather than matplotlib re-adjusting the box to the image's data
        # aspect ratio.
        ax.set_aspect("auto")
        ax.set_xticks([])
        ax.set_yticks([])

    # ---- right panel: depth-resolved trend ----
    if args.with_white:
        plot_curve(
            ax_curve, prof_white["ys"],
            [(10 ** (-o) if args.xquantity == "transmittance" else o)
             for o in prof_white["ods"]],
            COLORS["white"], "White (G ch.)")
    COLOR_NAME = {"625": "red", "525": "green", "465": "blue"}
    for label, photo, ch, col in color_specs:
        ys, ods = load_csv_channel(run_dir / "profiles.csv", col)
        vals = [(10 ** (-o) if args.xquantity == "transmittance" else o) for o in ods]
        plot_curve(ax_curve, ys, vals, COLORS[label], COLOR_NAME[label])

    if args.xquantity == "transmittance":
        ax_curve.set_xlim(0, 1.05)
        ax_curve.set_xticks([0, 0.5, 1.0])
        ax_curve.set_xlabel("Transmittance  T = 10$^{-OD}$")
    else:
        ax_curve.set_xlim(left=0)
        ax_curve.set_xlabel("Optical density (OD)")

    # y-axis on the left spine (standard); right spine hidden.
    ax_curve.yaxis.tick_left()
    ax_curve.yaxis.set_label_position("left")
    depth_label = ("Depth from meniscus (mm)" if args.mm_per_px
                   else "Depth from meniscus (px)")
    ax_curve.set_ylabel(depth_label, labelpad=3)
    tick_rows, tick_labels = make_depth_ticks(y0c, y1c, men_y, args.mm_per_px)
    ax_curve.set_yticks(tick_rows)
    ax_curve.set_yticklabels(tick_labels)
    ax_curve.tick_params(axis="y", pad=2)
    ax_curve.spines["right"].set_visible(False)
    ax_curve.spines["top"].set_visible(False)

    if args.with_white:
        ax_curve.legend(loc="lower center", frameon=False, handlelength=1.4,
                        fontsize=7, ncol=2, columnspacing=1.0,
                        borderaxespad=0.3)
    else:
        ax_curve.legend(loc="lower right", frameon=True, framealpha=1.0,
                        edgecolor="none", handlelength=1.4, fontsize=7,
                        borderaxespad=0.3)

    # the horizontal guide lines in (e) (meniscus / sediment front / liquid
    # bottom) have been removed entirely by design: (e) shows only the three
    # curves and the legend. men_y/bot_y/front_y themselves are still used
    # for the crop rectangle, the saturation table, and the caption.
    ax_curve.set_ylim(y1c, y0c)  # same orientation as extent (larger y = further down, toward the bottom)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    out_png = out_dir / f"{run_name}_panel.png"
    out_pdf = out_dir / f"{run_name}_panel.pdf"
    fig.savefig(out_png, dpi=300)
    fig.savefig(out_pdf)

    # ---- deterministic aspect-ratio check: rendered axes box geometry vs. the source crop ----
    # Uses the first photo axes' actual on-screen geometry (pixel width/
    # height of its bounding box), which is independent of the photo's
    # content, rather than trying to detect the photo in the rendered PNG.
    renderer = fig.canvas.get_renderer()
    bb = photo_axes[0][0].get_window_extent(renderer)
    rendered_ratio = bb.width / bb.height
    data_ratio = crop_w / crop_h
    rel_diff = abs(rendered_ratio - data_ratio) / data_ratio
    print(f"Aspect-ratio check: crop w/h={data_ratio:.4f}, "
          f"rendered w/h={rendered_ratio:.4f}, diff={rel_diff:.2%}")
    assert rel_diff < 0.01, (
        f"The rendered photo axes' aspect ratio differs from the source crop (crop_w/crop_h) by more than 1%: "
        f"diff={rel_diff:.2%} (data={data_ratio:.4f}, rendered={rendered_ratio:.4f})")

    plt.close(fig)

    print(f"\nWrote: {out_png} ({out_png.stat().st_size} bytes)")
    print(f"Wrote: {out_pdf} ({out_pdf.stat().st_size} bytes)")

    # ---- draft caption (from summary.json + the measured clip fractions) ----
    caption = build_caption(summary, run_name, run_dir, clip, args.with_white)
    out_caption = out_dir / f"{run_name}_caption.md"
    out_caption.write_text(caption, encoding="utf-8")
    print(f"Wrote: {out_caption} ({out_caption.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
