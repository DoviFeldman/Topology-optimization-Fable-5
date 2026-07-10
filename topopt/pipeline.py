"""End-to-end pipeline: STL in -> optimized smooth STL out.

Phases (reported via the progress callback):
    loading -> voxelizing -> optimizing -> meshing -> exporting
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Callable

import numpy as np
import trimesh
from scipy import ndimage

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
    fix_face: str = "bottom"  # clamped face(s); comma-separated, e.g. "left,right"
    load_face: str = "top"  # face whose voxels carry the load
    load_dir: str = "-z"  # force direction
    load_extent: float = 0.25  # fraction of the loaded face carrying force (1 = whole face)
    solid_load_face: bool = True  # keep the ENTIRE loaded face as a solid plate
    #   (the force still acts on the load_extent patch) — the mating surface
    #   survives even when the force is concentrated
    load_cases: int = 1  # 1, 3 or 5 force directions (tilted around load_dir);
    #   more cases = the part must resist wobble = cross-braced internal webbing
    shape_preserve: float = 0.0  # 0..1 density floor on the surface shell:
    #   keeps the input silhouette where it helps, opens up where it doesn't
    webbing_inside: bool = False  # forbid material on the outer skin (except
    #   contact pads): physics prefers a hollow tube, so without this the
    #   structure migrates outward; with it, webbing must form INSIDE
    domain_expand: float = 0.0  # 0..0.3: grow the buildable space outward by
    #   this fraction of the resolution, letting struts form outside the input
    rmin: float = 2.0  # density-filter radius, in voxels (1.5..2.5)
    min_feature_mm: float = 1.0  # printability: no strut thinner than this (mm);
    #   also sets the thickness of the solid pads kept at anchors and load
    rotation: str = ""  # optional row-major 3x3 matrix (JSON, 9 floats) applied
    #   to the input mesh before everything else — reorient without re-exporting
    max_iter: int = 60
    upsample: bool = True  # 2x trilinear upsample before marching cubes
    taubin_iterations: int = 20

    @property
    def fix_faces(self) -> tuple[str, ...]:
        return tuple(f.strip() for f in self.fix_face.split(",") if f.strip())

    def validate(self) -> None:
        if not 24 <= self.resolution <= 160:
            raise ValueError("resolution must be within 24..160")
        if not 0.1 <= self.volfrac <= 0.8:
            raise ValueError("volfrac must be within 0.1..0.8")
        fixes = self.fix_faces
        if not fixes or any(f not in FACES for f in fixes) or self.load_face not in FACES:
            raise ValueError(f"faces must be one of {FACES}")
        if self.load_face in fixes:
            raise ValueError("fixed face and loaded face must differ")
        if self.load_dir not in DIRECTIONS:
            raise ValueError(f"load_dir must be one of {sorted(DIRECTIONS)}")
        if not 0.05 <= self.load_extent <= 1.0:
            raise ValueError("load_extent must be within 0.05..1.0")
        if self.load_cases not in (1, 3, 5):
            raise ValueError("load_cases must be 1, 3 or 5")
        if not 0.0 <= self.shape_preserve <= 1.0:
            raise ValueError("shape_preserve must be within 0..1")
        if not 0.0 <= self.domain_expand <= 0.3:
            raise ValueError("domain_expand must be within 0..0.3")
        if not 1.0 <= self.rmin <= 4.0:
            raise ValueError("rmin must be within 1.0..4.0")
        if not 0.0 <= self.min_feature_mm <= 5.0:
            raise ValueError("min_feature_mm must be within 0..5")
        if self.rotation:
            try:
                r = np.array(json.loads(self.rotation), dtype=float)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"rotation is not a valid JSON matrix: {exc}")
            if r.shape != (9,) and r.shape != (3, 3):
                raise ValueError("rotation must contain 9 numbers (row-major 3x3)")
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


def face_voxels(occ: np.ndarray, face: str, extent: float = 1.0) -> np.ndarray:
    """Select the layer of occupied voxels forming the given bounding-box face.

    Loads/supports snap to the nearest occupied voxel: for every grid column
    perpendicular to the face, take the first occupied voxel seen from that
    face — but only for columns whose surface lies close to the face plane
    (within ~8% of the axis extent), so e.g. the "top" of an L-bracket is the
    top of its vertical leg, not the lower horizontal leg.

    `extent < 1` keeps only a centered elliptical patch of the face, sized as
    that fraction of the face footprint. Concentrated loads produce distinct
    force paths (branching struts) instead of a uniform slab.
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

    a, b = np.nonzero(cols)
    depth = first[cols]
    if extent < 1.0:
        ca, cb = a.mean(), b.mean()
        ha = max((a.max() - a.min()) / 2.0, 1.0)
        hb = max((b.max() - b.min()) / 2.0, 1.0)
        d2 = ((a - ca) / ha) ** 2 + ((b - cb) / hb) ** 2
        keep = d2 <= extent**2
        if keep.sum() < 4:  # never let the patch vanish on tiny/odd footprints
            keep = np.argsort(d2)[: min(4, d2.size)]
        a, b, depth = a[keep], b[keep], depth[keep]

    mask_view = np.zeros_like(view)
    mask_view[depth, a, b] = True
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
    if params.rotation:
        t = np.eye(4)
        t[:3, :3] = np.array(json.loads(params.rotation), dtype=float).reshape(3, 3)
        mesh.apply_transform(t)
    n_input_faces = len(mesh.faces)

    report("voxelizing", message=f"voxelizing {n_input_faces} triangles")
    grid = voxelize_solid(mesh, params.resolution)
    del mesh  # the input mesh plays no further role — free the memory

    # optionally grow the buildable space outward, so the optimizer may place
    # struts outside the input silhouette (the shell floor below still refers
    # to the *original* surface)
    original_occ = grid.occ
    if params.domain_expand > 0:
        n = max(1, int(round(params.domain_expand * params.resolution)))
        original_occ = np.pad(grid.occ, n)
        grid = VoxelGrid(
            occ=ndimage.binary_dilation(original_occ, iterations=n),
            pitch=grid.pitch,
            origin=grid.origin - n * grid.pitch,
        )

    # shape preservation: a density floor on the input's 2-voxel surface shell.
    # The optimizer must keep at least this much material there, so the
    # silhouette survives where it carries load and erodes where it doesn't
    # (below ~0.5 the shell falls under the meshing iso-level and opens up).
    floor_vec = None
    shell = original_occ & ~ndimage.binary_erosion(original_occ, iterations=2)
    if params.shape_preserve > 0 and not params.webbing_inside:
        floor_vec = np.where(shell, float(params.shape_preserve), 0.0)[grid.occ]

    # webbing_inside is the opposite of shape preservation: a density CEILING
    # below the meshing iso-level on that same shell, so the outer skin (minus
    # the contact pads, which stay passive-solid) can never materialize —
    # structure is forced to form as internal webbing instead of the hollow
    # tube that pure physics prefers.
    ceiling_vec = None
    if params.webbing_inside:
        ceiling_vec = np.where(shell, 0.25, 1.0)[grid.occ]

    fixed_vox = np.zeros_like(grid.occ)
    for f in params.fix_faces:
        fixed_vox |= face_voxels(grid.occ, f)
    load_vox = face_voxels(grid.occ, params.load_face, params.load_extent)
    overlap = fixed_vox & load_vox
    if overlap.any():
        load_vox &= ~overlap

    # printability: no strut thinner than min_feature_mm. The density filter's
    # radius sets the minimum member size (~2 * rmin voxels), so give it a
    # floor in voxel units derived from the physical requirement.
    rmin_eff = max(params.rmin, 0.5 * params.min_feature_mm / grid.pitch)

    # contact pads: the anchored faces and the loaded surface stay fully solid
    # for at least min_feature_mm of depth, so the part still sits flat where
    # it used to and the force has a solid plate to connect to — the optimizer
    # may only carve *between* the pads, never the pads themselves. With
    # solid_load_face the pad covers the whole loaded face (not just the force
    # patch).
    if not params.solid_load_face:
        load_pad = load_vox
    else:
        load_pad = face_voxels(grid.occ, params.load_face, 1.0)
    pad_it = max(2, int(np.ceil(params.min_feature_mm / grid.pitch)))
    passive_grid = (
        ndimage.binary_dilation(fixed_vox, iterations=pad_it)
        | ndimage.binary_dilation(load_pad, iterations=pad_it)
    ) & grid.occ

    # load cases: the main direction plus tilted companions. A design that
    # must resist several force directions at once develops cross-braced
    # webbing instead of a single strut path.
    d = np.array(DIRECTIONS[params.load_dir], dtype=float)
    dirs, weights = [d], [1.0]
    if params.load_cases > 1:
        helper = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        u = np.cross(d, helper)
        u /= np.linalg.norm(u)
        v = np.cross(d, u)
        tilt = np.tan(np.radians(35.0))
        for s in [u, -u] if params.load_cases == 3 else [u, -u, v, -v]:
            w = d + tilt * s
            dirs.append(w / np.linalg.norm(w))
            weights.append(0.4)

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
        np.array(dirs),
        volfrac=params.volfrac,
        rmin=rmin_eff,
        max_iter=params.max_iter,
        case_weights=np.array(weights),
        min_density=floor_vec,
        max_density=ceiling_vec,
        passive=passive_grid,
        progress=simp_progress,
    )

    report("meshing", message="marching cubes + Taubin smoothing")
    # fine lattices get gentler smoothing — full-strength blur + Taubin wipes
    # out struts only a couple voxels thick, exactly the ones that make the
    # result look webby
    out_mesh, info = density_to_mesh(
        simp.density_grid,
        grid,
        upsample=params.upsample,
        gaussian_sigma=min(1.0, max(0.5, 0.5 * rmin_eff)),
        taubin_iterations=(
            params.taubin_iterations if rmin_eff >= 1.6
            else max(8, params.taubin_iterations // 2)
        ),
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
