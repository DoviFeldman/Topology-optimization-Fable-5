"""End-to-end pipeline: STL in -> optimized smooth STL out.

Phases (reported via the progress callback):
    loading -> voxelizing -> optimizing -> meshing -> exporting
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Callable

import numpy as np
import trimesh

from .meshing import density_to_mesh
from .simp import simp_optimize
from .voxelize import VoxelGrid, load_mesh, voxelize_solid

FACES = ("bottom", "top", "left", "right", "front", "back")
DIRECTIONS = {
    "+x": (1.0, 0.0, 0.0),
    "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0),
    "-z": (0.0, 0.0, -1.0),
}
# face name -> (axis, side): side 'low' means the min end of that axis
_FACE_AXIS = {
    "left": (0, "low"),
    "right": (0, "high"),
    "front": (1, "low"),
    "back": (1, "high"),
    "bottom": (2, "low"),
    "top": (2, "high"),
}

ProgressFn = Callable[[str, dict], None]
"""(phase, detail) — detail may hold iteration, total, compliance, message."""


@dataclass
class Params:
    """User-facing knobs, all with safe defaults (one-click friendly)."""

    resolution: int = 96  # voxels along the longest axis (48..160 typical)
    volfrac: float = 0.35  # fraction of material to keep
    fix_face: str = "bottom"  # face whose voxels are clamped
    load_face: str = "top"  # face whose voxels carry the load
    load_dir: str = "-z"  # force direction
    rmin: float = 2.0  # density-filter radius, in voxels (1.5..2.5)
    max_iter: int = 60
    upsample: bool = True  # 2x trilinear upsample before marching cubes
    taubin_iterations: int = 20

    def validate(self) -> None:
        if not 24 <= self.resolution <= 160:
            raise ValueError("resolution must be within 24..160")
        if not 0.1 <= self.volfrac <= 0.8:
            raise ValueError("volfrac must be within 0.1..0.8")
        if self.fix_face not in FACES or self.load_face not in FACES:
            raise ValueError(f"faces must be one of {FACES}")
        if self.fix_face == self.load_face:
            raise ValueError("fixed face and loaded face must differ")
        if self.load_dir not in DIRECTIONS:
            raise ValueError(f"load_dir must be one of {sorted(DIRECTIONS)}")
        if not 1.0 <= self.rmin <= 4.0:
            raise ValueError("rmin must be within 1.0..4.0")
        if not 5 <= self.max_iter <= 200:
            raise ValueError("max_iter must be within 5..200")


@dataclass
class PipelineResult:
    output_path: str
    watertight: bool
    iterations: int
    converged: bool
    compliance_history: list[float] = field(default_factory=list)
    volume_fraction: float = 0.0
    vertices: int = 0
    faces: int = 0
    taubin_iterations: int = 0
    n_voxels: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def face_voxels(occ: np.ndarray, face: str) -> np.ndarray:
    """Select the layer of occupied voxels forming the given bounding-box face.

    Loads/supports snap to the nearest occupied voxel: for every grid column
    perpendicular to the face, take the first occupied voxel seen from that
    face — but only for columns whose surface lies close to the face plane
    (within ~8% of the axis extent), so e.g. the "top" of an L-bracket is the
    top of its vertical leg, not the lower horizontal leg.
    """
    axis, side = _FACE_AXIS[face]
    view = np.moveaxis(occ, axis, 0)
    if side == "high":
        view = view[::-1]
    n = view.shape[0]
    has_any = view.any(axis=0)
    if not has_any.any():
        raise ValueError("no occupied voxels on the requested face")
    first = np.argmax(view, axis=0)  # first occupied layer per column
    ref = int(first[has_any].min())
    tol = max(2, int(round(0.08 * n)))
    cols = has_any & (first <= ref + tol)
    mask_view = np.zeros_like(view)
    a, b = np.nonzero(cols)
    mask_view[first[cols], a, b] = True
    if side == "high":
        mask_view = mask_view[::-1]
    return np.ascontiguousarray(np.moveaxis(mask_view, 0, axis))


def run_pipeline(
    input_path: str,
    output_path: str,
    params: Params | None = None,
    progress: ProgressFn | None = None,
) -> PipelineResult:
    """Run the full optimization and write a binary STL to `output_path`."""
    params = params or Params()
    params.validate()

    def report(phase: str, **detail) -> None:
        if progress is not None:
            progress(phase, detail)

    report("loading", message="reading and repairing input mesh")
    mesh = load_mesh(input_path)
    n_input_faces = len(mesh.faces)

    report("voxelizing", message=f"voxelizing {n_input_faces} triangles")
    grid = voxelize_solid(mesh, params.resolution)
    del mesh  # the input mesh plays no further role — free the memory

    fixed_vox = face_voxels(grid.occ, params.fix_face)
    load_vox = face_voxels(grid.occ, params.load_face)
    overlap = fixed_vox & load_vox
    if overlap.any():
        load_vox &= ~overlap

    def simp_progress(it: int, total: int, compliance: float, change: float) -> None:
        report(
            "optimizing",
            iteration=it,
            total=total,
            compliance=compliance,
            change=change,
        )

    report("optimizing", iteration=0, total=params.max_iter, message="starting SIMP")
    simp = simp_optimize(
        grid.occ,
        fixed_vox,
        load_vox,
        np.array(DIRECTIONS[params.load_dir]),
        volfrac=params.volfrac,
        rmin=params.rmin,
        max_iter=params.max_iter,
        progress=simp_progress,
    )

    report("meshing", message="marching cubes + Taubin smoothing")
    out_mesh, info = density_to_mesh(
        simp.density_grid,
        grid,
        upsample=params.upsample,
        taubin_iterations=params.taubin_iterations,
    )

    report("exporting", message="writing binary STL")
    out_mesh.export(output_path, file_type="stl")

    return PipelineResult(
        output_path=output_path,
        watertight=info.watertight,
        iterations=simp.iterations,
        converged=simp.converged,
        compliance_history=simp.compliance_history,
        volume_fraction=simp.volume_fraction,
        vertices=info.vertices,
        faces=info.faces,
        taubin_iterations=info.taubin_iterations,
        n_voxels=grid.n_solid,
    )
