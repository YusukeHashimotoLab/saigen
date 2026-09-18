# SPDX-License-Identifier: MIT
"""Render the preview images shown in cad/README.md.

Every ``cad/**/*.stl`` is rendered from the same isometric viewpoint with the
same lighting to ``cad/previews/<file stem>.png``; assemblies listed in
``ASSEMBLIES`` are rendered as well. Re-run after adding or changing a part:

    pip install pyvista pillow
    python cad/tools/render_previews.py

The script is a documentation helper. Nothing in ``src/`` depends on it, and
pyvista is deliberately not part of ``requirements.txt``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyvista as pv
from PIL import Image

CAD = Path(__file__).resolve().parents[1]
OUT = CAD / "previews"

BACKGROUND = "#f3f5f7"   # light card; reads on GitHub's light and dark themes
PART = "#9fb2c6"
PART_2 = "#d9b38c"       # second part of an assembly
EDGE = "#2f3b47"
RENDER = (1600, 1200)    # rendered large, then reduced for anti-aliasing
FINAL = (640, 480)
VIEW = (1.0, -1.25, 0.85)  # camera direction, Z up (Fusion 360 STL export)


def _flip_z(height: float, lift: float) -> np.ndarray:
    """Turn a part upside down about X and put its lowest face at z = lift."""
    m = np.eye(4)
    m[1, 1] = m[2, 2] = -1.0
    m[2, 3] = height + lift
    return m


# name -> [(stl relative to cad/, 4x4 transform or None, colour)]
ASSEMBLIES = {
    # The posts of the upper frame slide down the corner grooves of the base
    # and rest on the 9 mm corner ledges, ring uppermost.
    "vessel_holder_assembly_500ml": [
        ("vessel-holder/vessel_holder_bottom_500ml.stl", None, PART),
        ("vessel-holder/vessel_holder_top_500ml.stl", _flip_z(160.0, 9.0), PART_2),
    ],
}


def render(parts, out: Path) -> None:
    plotter = pv.Plotter(off_screen=True, window_size=RENDER, lighting="three lights")
    plotter.set_background(BACKGROUND)
    for mesh, colour in parts:
        plotter.add_mesh(mesh, color=colour, smooth_shading=True, split_sharp_edges=True,
                         feature_angle=35, ambient=0.25, diffuse=0.75, specular=0.15)
        edges = mesh.extract_feature_edges(feature_angle=35, boundary_edges=False,
                                           non_manifold_edges=False, manifold_edges=False)
        if edges.n_cells:
            plotter.add_mesh(edges, color=EDGE, line_width=2.5)
    plotter.view_vector(VIEW, viewup=(0, 0, 1))
    plotter.camera.parallel_projection = True
    plotter.reset_camera()
    plotter.camera.zoom(1.0)  # larger values crop the tall parts
    image = Image.fromarray(plotter.screenshot(return_img=True))
    plotter.close()
    image = image.resize(FINAL, Image.LANCZOS).quantize(colors=96, dither=Image.Dither.NONE)
    image.save(out, optimize=True)
    print(f"{out.relative_to(CAD.parent)}  {out.stat().st_size / 1024:.0f} kB")


def main() -> int:
    OUT.mkdir(exist_ok=True)
    for stl in sorted(CAD.rglob("*.stl")):
        render([(pv.read(stl), PART)], OUT / f"{stl.stem}.png")
    for name, members in ASSEMBLIES.items():
        parts = []
        for rel, transform, colour in members:
            mesh = pv.read(CAD / rel)
            if transform is not None:
                mesh = mesh.transform(transform, inplace=False)
            parts.append((mesh, colour))
        render(parts, OUT / f"{name}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
