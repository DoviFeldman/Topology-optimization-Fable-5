#!/usr/bin/env python3
"""Generate test STL files into ./test_shapes/.

    python make_test_shapes.py          # box, box_with_hole, l_bracket
    python make_test_shapes.py --big    # also big_box_with_hole (>1M triangles)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import trimesh
from shapely.geometry import Polygon

OUT_DIR = Path(__file__).parent / "test_shapes"


def make_box() -> trimesh.Trimesh:
    """Simple 60 x 40 x 30 mm box."""
    return trimesh.creation.box(extents=[60.0, 40.0, 30.0])


def make_box_with_hole() -> trimesh.Trimesh:
    """60 x 60 x 25 mm plate with a 15 mm-radius through-hole."""
    outer = [(-30, -30), (30, -30), (30, 30), (-30, 30)]
    hole = [
        (15 * np.cos(t), 15 * np.sin(t))
        for t in np.linspace(0, 2 * np.pi, 48, endpoint=False)
    ]
    poly = Polygon(outer, [hole[::-1]])  # interior ring wound opposite
    mesh = trimesh.creation.extrude_polygon(poly, height=25.0)
    return mesh


def make_l_bracket() -> trimesh.Trimesh:
    """Classic L-bracket: 80 x 80 mm legs, 30 mm thick legs, 20 mm deep."""
    pts = [(0, 0), (80, 0), (80, 30), (30, 30), (30, 80), (0, 80)]
    mesh = trimesh.creation.extrude_polygon(Polygon(pts), height=20.0)
    # stand it up: extrusion depth (z) becomes y so the L faces the viewer
    mesh.apply_transform(
        trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0])
    )
    return mesh


def make_big(base: trimesh.Trimesh, min_faces: int = 1_000_000) -> trimesh.Trimesh:
    """Subdivide a mesh until it exceeds `min_faces` triangles."""
    mesh = base.copy()
    while len(mesh.faces) < min_faces:
        mesh = mesh.subdivide()
    return mesh


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--big", action="store_true",
        help="also generate a >1M-triangle STL (large file, ~100 MB)",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    shapes = {
        "box.stl": make_box(),
        "box_with_hole.stl": make_box_with_hole(),
        "l_bracket.stl": make_l_bracket(),
    }
    if args.big:
        shapes["big_box_with_hole.stl"] = make_big(make_box_with_hole())

    for name, mesh in shapes.items():
        path = OUT_DIR / name
        mesh.export(path)
        print(
            f"wrote {path}  ({len(mesh.faces):,} triangles, "
            f"watertight={mesh.is_watertight})"
        )


if __name__ == "__main__":
    main()
