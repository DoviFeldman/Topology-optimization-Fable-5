#!/usr/bin/env python3
"""Generate the webbing gallery: one tall cylinder pedestal (r=15, h=65 mm,
anchored at the bottom, weight pressing down on the top plate), optimized 10
ways. Outputs STLs + manifest.json into static/gallery/ for gallery.html.

    python make_gallery.py            # ~1.5-2 h on 2 cores, minutes on an M1
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import trimesh

from topopt.pipeline import Params, run_pipeline

OUT = Path(__file__).parent / "static" / "gallery"
CYL = OUT / "_input_cylinder.stl"

# name, blurb, params (all share: fix bottom, load top, dir -z, 1 mm min strut)
CONFIGS = [
    ("01-inside-woven-lite", "Inside-only webbing, 5 force directions, 15% material — max lace",
     dict(webbing_inside=True, load_cases=5, volfrac=0.15, load_extent=0.2, rmin=1.3, resolution=44, max_iter=30)),
    ("02-inside-woven-mid", "Inside-only webbing, 5 directions, 22% material — lace with more meat",
     dict(webbing_inside=True, load_cases=5, volfrac=0.22, load_extent=0.25, rmin=1.3, resolution=44, max_iter=30)),
    ("03-inside-crossed", "Inside-only, 3 directions, 18% — cross-braced classic",
     dict(webbing_inside=True, load_cases=3, volfrac=0.18, load_extent=0.3, rmin=1.4, resolution=48, max_iter=30)),
    ("04-inside-widegrip", "Inside-only, force spread across most of the top — legs at the rim",
     dict(webbing_inside=True, load_cases=5, volfrac=0.20, load_extent=0.8, rmin=1.3, resolution=44, max_iter=30)),
    ("05-free-tube", "BASELINE without inside-mode: physics picks a hollow tube (compare!)",
     dict(webbing_inside=False, load_cases=5, volfrac=0.18, load_extent=0.2, rmin=1.3, resolution=44, max_iter=30)),
    ("06-shell-windows", "55% skin preserved: the tube look with organic windows",
     dict(webbing_inside=False, shape_preserve=0.55, load_cases=3, volfrac=0.18, load_extent=0.25, rmin=1.5, resolution=48, max_iter=30)),
    ("07-inside-hero-fine", "The hero: inside-only, 5 directions, finest filter, res 60",
     dict(webbing_inside=True, load_cases=5, volfrac=0.18, load_extent=0.25, rmin=1.2, resolution=60, max_iter=40)),
    ("08-inside-organic-thick", "Inside-only but chunky: 28% material, soft filter — organic bones",
     dict(webbing_inside=True, load_cases=3, volfrac=0.28, load_extent=0.4, rmin=1.8, resolution=48, max_iter=30)),
    ("09-inside-super-skeletal", "Inside-only, 12% material — as little as it can stand",
     dict(webbing_inside=True, load_cases=5, volfrac=0.12, load_extent=0.15, rmin=1.2, resolution=44, max_iter=30)),
    ("10-inside-buttress", "Inside-only + 8% extra build room — webbing may bulge outward",
     dict(webbing_inside=True, domain_expand=0.08, load_cases=3, volfrac=0.20, load_extent=0.3, rmin=1.4, resolution=44, max_iter=28)),
]


def metrics(path: Path) -> dict:
    """Web-ness numbers: genus (through-holes) and how much of the mid-band
    material sits in the inner 70% radius (tube ≈ 0, internal webbing ≈ 1)."""
    m = trimesh.load(path)
    genus = int((2 - m.euler_number) // 2)
    v = m.voxelized(pitch=float(max(m.extents)) / 48).fill()
    occ = v.matrix
    nz = occ.shape[2]
    band = occ[:, :, int(0.25 * nz):int(0.75 * nz)]  # skip top/bottom pads
    cx, cy = (band.shape[0] - 1) / 2, (band.shape[1] - 1) / 2
    xi, yi = np.meshgrid(np.arange(band.shape[0]), np.arange(band.shape[1]), indexing="ij")
    r = np.sqrt((xi - cx) ** 2 + (yi - cy) ** 2)
    rmax = r[band.any(axis=2)].max() if band.any() else 1.0
    inner = band & (r < 0.7 * rmax)[:, :, None]
    total = int(band.sum())
    return {
        "genus": genus,
        "inner_fraction": round(float(inner.sum()) / total, 3) if total else 0.0,
        "volume_pct": round(float(m.volume) / (np.pi * 15**2 * 65) * 100, 1),
        "faces": len(m.faces),
        "watertight": bool(m.is_watertight),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    trimesh.creation.cylinder(radius=15.0, height=65.0, sections=64).export(CYL)
    manifest = {"input": "cylinder r=15mm h=65mm, anchored bottom, load pushes down on top plate",
                "models": []}
    for name, blurb, overrides in CONFIGS:
        out_path = OUT / f"{name}.stl"
        params = Params(fix_face="bottom", load_face="top", load_dir="-z",
                        min_feature_mm=1.0, **overrides)
        entry = {"name": name, "blurb": blurb, "file": out_path.name,
                 "settings": overrides}
        t0 = time.time()
        try:
            result = run_pipeline(str(CYL), str(out_path), params)
            entry["metrics"] = metrics(out_path)
            entry["iterations"] = result.iterations
            entry["seconds"] = round(time.time() - t0, 1)
            print(f"[done] {name}  {entry['seconds']}s  {entry['metrics']}", flush=True)
        except Exception as exc:  # record and continue
            entry["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[FAIL] {name}: {entry['error']}", flush=True)
        manifest["models"].append(entry)
        (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("gallery complete", flush=True)


if __name__ == "__main__":
    main()
