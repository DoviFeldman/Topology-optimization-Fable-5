"""Density field -> smooth, watertight, printable mesh.

The "money step": the continuous density field is upsampled with trilinear
interpolation, lightly Gaussian-blurred, contoured with marching cubes at the
0.5 iso-level, then Taubin-smoothed (a shrinkage-free two-pass Laplacian).
The output therefore shows no voxel staircase. The mesh is reduced to its
largest connected component and repaired until watertight before export.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import trimesh
from scipy import ndimage
from skimage import measure

from .voxelize import VoxelGrid


@dataclass
class MeshingInfo:
    watertight: bool
    taubin_iterations: int
    upsampled: bool
    components_removed: int
    vertices: int
    faces: int


def density_to_mesh(
    density_grid: np.ndarray,
    grid: VoxelGrid,
    upsample: bool = True,
    gaussian_sigma: float = 1.0,
    taubin_iterations: int = 20,
) -> tuple[trimesh.Trimesh, MeshingInfo]:
    """Extract a smooth world-space surface from a [0, 1] density grid.

    `density_grid` has the same shape as `grid.occ`; value at index (i,j,k)
    is the density of the voxel centered at grid coordinate (i+.5, j+.5, k+.5).
    """
    pad = 2
    field = np.pad(density_grid.astype(np.float32), pad, mode="constant")
    envelope = np.pad(grid.occ.astype(np.float32), pad, mode="constant")

    # guard: if optimization left everything below the iso level, renormalize
    peak = float(field.max())
    if peak <= 0.0:
        raise ValueError("density field is empty — nothing to mesh")
    if peak < 0.55:
        field = field * (0.75 / peak)

    scale_back = np.ones(3)
    if upsample:
        old_shape = np.array(field.shape)
        field = ndimage.zoom(field, 2.0, order=1)  # trilinear
        envelope = ndimage.zoom(envelope, 2.0, order=1)
        new_shape = np.array(field.shape)
        # zoom maps index endpoints proportionally: up_idx * (N-1)/(M-1) = idx
        scale_back = (old_shape - 1) / np.maximum(new_shape - 1, 1)

    if gaussian_sigma > 0:
        field = ndimage.gaussian_filter(field, sigma=gaussian_sigma)
        envelope = ndimage.gaussian_filter(envelope, sigma=gaussian_sigma)

    # Clamp to the input's (smoothed) envelope: the blur otherwise bleeds
    # density outward and the result would overshoot the original bounds by
    # about a voxel. The envelope field crosses 0.5 at the input surface, so
    # the output never grows past the part it was carved from.
    np.minimum(field, envelope, out=field)

    verts, faces, _, _ = measure.marching_cubes(field, level=0.5)
    verts = verts * scale_back[None, :]  # back to padded-grid index space
    verts = grid.to_world(verts - pad + 0.5)  # index -> voxel center -> world

    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)

    n_removed = 0
    components = mesh.split(only_watertight=False)
    if len(components) > 1:
        n_removed = len(components) - 1
        components = sorted(components, key=lambda m: abs(m.volume), reverse=True)
        mesh = components[0]

    applied_taubin = 0
    if taubin_iterations > 0 and len(mesh.vertices) > 0:
        trimesh.smoothing.filter_taubin(mesh, lamb=0.5, nu=-0.53, iterations=taubin_iterations)
        applied_taubin = taubin_iterations

    mesh = _make_watertight(mesh)

    info = MeshingInfo(
        watertight=bool(mesh.is_watertight),
        taubin_iterations=applied_taubin,
        upsampled=upsample,
        components_removed=n_removed,
        vertices=len(mesh.vertices),
        faces=len(mesh.faces),
    )
    return mesh, info


def _make_watertight(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Repair until watertight: drop degenerate faces, merge, fill holes."""
    if mesh.is_watertight:
        trimesh.repair.fix_normals(mesh)
        return mesh
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices()
    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
    trimesh.repair.fix_normals(mesh)
    if not mesh.is_watertight:
        # last resort: keep the largest strictly-watertight body if one exists
        bodies = [m for m in mesh.split(only_watertight=True) if len(m.faces) > 0]
        if bodies:
            mesh = max(bodies, key=lambda m: abs(m.volume))
            trimesh.repair.fix_normals(mesh)
    return mesh
