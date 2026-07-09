#!/usr/bin/env python3
"""Command-line topology optimizer: STL in, smooth optimized STL out.

Example:
    python optimize.py input.stl output.stl --resolution 96 --volfrac 0.35 \
        --fix bottom --load top --dir -z
"""

from __future__ import annotations

import argparse
import sys
import time

import trimesh

from topopt.pipeline import DIRECTIONS, FACES, Params, run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SIMP topology optimization: STL -> smooth printable STL",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="input STL file (binary or ASCII)")
    parser.add_argument("output", help="output STL file (binary)")
    parser.add_argument(
        "--resolution", type=int, default=96,
        help="voxels along the longest axis (24..160)",
    )
    parser.add_argument(
        "--volfrac", type=float, default=0.35,
        help="fraction of material to keep (0.1..0.8)",
    )
    parser.add_argument("--fix", choices=FACES, default="bottom", help="fixed (support) face")
    parser.add_argument("--load", choices=FACES, default="top", help="loaded face")
    parser.add_argument(
        "--dir", choices=sorted(DIRECTIONS), default="-z", dest="load_dir",
        help="load direction",
    )
    parser.add_argument("--rmin", type=float, default=2.0, help="filter radius in voxels")
    parser.add_argument("--max-iter", type=int, default=60, help="max SIMP iterations")
    parser.add_argument(
        "--no-upsample", action="store_true",
        help="skip the 2x density upsample before marching cubes",
    )
    parser.add_argument(
        "--smooth-iter", type=int, default=20, help="Taubin smoothing iterations"
    )
    args = parser.parse_args(argv)

    params = Params(
        resolution=args.resolution,
        volfrac=args.volfrac,
        fix_face=args.fix,
        load_face=args.load,
        load_dir=args.load_dir,
        rmin=args.rmin,
        max_iter=args.max_iter,
        upsample=not args.no_upsample,
        taubin_iterations=args.smooth_iter,
    )

    t0 = time.time()
    last_phase = [""]

    def progress(phase: str, detail: dict) -> None:
        if phase != last_phase[0]:
            print(f"[{time.time() - t0:7.1f}s] phase: {phase}", flush=True)
            last_phase[0] = phase
        if phase == "optimizing" and detail.get("iteration"):
            print(
                f"[{time.time() - t0:7.1f}s]   iter {detail['iteration']:3d}/"
                f"{detail['total']} | compliance {detail['compliance']:.4e} | "
                f"change {detail['change']:.3f}",
                flush=True,
            )

    result = run_pipeline(args.input, args.output, params, progress)

    print(f"[{time.time() - t0:7.1f}s] done: {result.output_path}")
    print(
        f"  iterations: {result.iterations} ({'converged' if result.converged else 'max iterations'})\n"
        f"  volume fraction achieved: {result.volume_fraction:.3f}\n"
        f"  output mesh: {result.vertices} vertices, {result.faces} faces\n"
        f"  watertight: {result.watertight}"
    )
    check = trimesh.load(args.output)
    print(f"  verified watertight on reload: {check.is_watertight}")
    return 0 if result.watertight else 1


if __name__ == "__main__":
    sys.exit(main())
