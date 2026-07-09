"""STL loading, repair, and solid voxelization.

The voxelizer is deliberately independent of input mesh quality: triangle
surfaces are point-sampled into a voxel shell (memory- and time-bounded even
for multi-million-triangle inputs), then the solid interior is recovered with
an outside flood fill. Meshes with small holes still voxelize correctly; if a
leak is detected the shell is morphologically closed and the fill re-run.

All downstream stages work in voxel-index space (one element == one unit
cube), which doubles as internal scale normalization: models of any physical
size are handled identically, and `VoxelGrid.to_world` restores the original
scale and position on export.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import trimesh
from scipy import ndimage

# Bound on the number of surface sample points processed per numpy batch.
_BATCH_POINTS = 4_000_000


@dataclass
class VoxelGrid:
    """Solid occupancy grid plus the transform back to world space."""

    occ: np.ndarray  # (nx, ny, nz) bool
    pitch: float  # world size of one voxel
    origin: np.ndarray  # world position of the corner of voxel (0, 0, 0)

    def to_world(self, points_vox: np.ndarray) -> np.ndarray:
        """Map voxel-index-space points (voxel centers at i+0.5) to world."""
        return self.origin[None, :] + points_vox * self.pitch

    @property
    def n_solid(self) -> int:
        return int(self.occ.sum())


def load_mesh(path_or_file, file_type: str | None = None) -> trimesh.Trimesh:
    """Load an STL (binary or ASCII) and attempt light repair.

    Repair failures are non-fatal: the voxelizer tolerates imperfect input.
    """
    mesh = trimesh.load(path_or_file, file_type=file_type, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise ValueError("input file contains no triangle geometry")
    mesh.remove_infinite_values()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    if len(mesh.faces) == 0:
        raise ValueError("input mesh has no valid (non-degenerate) triangles")
    if not mesh.is_watertight:
        try:
            trimesh.repair.fill_holes(mesh)
            trimesh.repair.fix_normals(mesh)
        except Exception:
            pass  # best effort — solid fill below survives imperfect meshes
    return mesh


def voxelize_solid(mesh: trimesh.Trimesh, resolution: int = 96) -> VoxelGrid:
    """Voxelize a mesh into a *solid* occupancy grid.

    `resolution` is the number of voxels along the longest bounding-box axis.
    A one-voxel empty margin surrounds the model so the flood fill can reach
    every outside region.
    """
    if resolution < 16 or resolution > 256:
        raise ValueError("resolution must be within 16..256")
    extents = mesh.bounds[1] - mesh.bounds[0]
    max_extent = float(extents.max())
    if max_extent <= 0:
        raise ValueError("mesh has zero extent")
    pitch = max_extent / resolution
    margin = 1
    dims = np.maximum(np.ceil(extents / pitch).astype(int) + 2 * margin, 3)
    origin = mesh.bounds[0] - margin * pitch

    shell = _sample_shell(mesh, origin, pitch, dims)
    solid = _fill_interior(shell)
    solid = _largest_component(solid)
    if not solid.any():
        raise ValueError("voxelization produced an empty grid")
    return VoxelGrid(occ=solid, pitch=pitch, origin=origin.astype(float))


def _sample_shell(
    mesh: trimesh.Trimesh, origin: np.ndarray, pitch: float, dims: np.ndarray
) -> np.ndarray:
    """Mark every voxel touched by the surface, by point-sampling triangles.

    Triangles are grouped by required subdivision level so each group is one
    vectorized barycentric evaluation; groups are processed in slabs bounded
    by `_BATCH_POINTS` so memory stays flat even for 1-2M-triangle meshes.
    """
    shell = np.zeros(tuple(dims), dtype=bool)
    tri_all = mesh.triangles  # (n, 3, 3)
    n_tri = len(tri_all)
    chunk = 200_000
    for start in range(0, n_tri, chunk):
        tri = np.asarray(tri_all[start : start + chunk], dtype=np.float64)
        edges = np.stack(
            [
                np.linalg.norm(tri[:, 1] - tri[:, 0], axis=1),
                np.linalg.norm(tri[:, 2] - tri[:, 1], axis=1),
                np.linalg.norm(tri[:, 2] - tri[:, 0], axis=1),
            ],
            axis=1,
        ).max(axis=1)
        # sample spacing ~ pitch/2 so no voxel crossed by a triangle is missed
        level = np.ceil(edges / (0.5 * pitch)).astype(np.int64) + 1
        np.clip(level, 1, 512, out=level)
        for lv in np.unique(level):
            group = tri[level == lv]
            bary = _bary_grid(int(lv))
            pts_per_tri = len(bary)
            step = max(1, _BATCH_POINTS // pts_per_tri)
            for gs in range(0, len(group), step):
                pts = np.einsum(
                    "sb,tbc->tsc", bary, group[gs : gs + step]
                ).reshape(-1, 3)
                idx = np.floor((pts - origin[None, :]) / pitch).astype(np.int64)
                np.clip(idx, 0, np.asarray(dims)[None, :] - 1, out=idx)
                shell[idx[:, 0], idx[:, 1], idx[:, 2]] = True
    return shell


def _bary_grid(level: int) -> np.ndarray:
    """Barycentric coordinates of a uniform triangular grid with `level` rows."""
    coords = []
    for i in range(level + 1):
        for j in range(level + 1 - i):
            coords.append((i / level, j / level, 1.0 - (i + j) / level))
    return np.asarray(coords, dtype=np.float64)


def _fill_interior(shell: np.ndarray) -> np.ndarray:
    """Solidify a voxel shell by flood-filling from outside.

    If the shell leaks (holes in the input surface), apply progressively
    stronger morphological closing and retry.
    """
    solid = _flood(shell)
    interior = int(solid.sum()) - int(shell.sum())
    if interior > 0:
        return solid
    for close_iters in (1, 2, 3):
        closed = ndimage.binary_closing(shell, iterations=close_iters)
        solid = _flood(closed | shell)
        if int(solid.sum()) - int((closed | shell).sum()) > 0:
            return solid
    # Shell might legitimately have no interior (very thin part) — keep it.
    return solid


def _flood(shell: np.ndarray) -> np.ndarray:
    empty = ~shell
    labels, _ = ndimage.label(empty)
    border = np.unique(
        np.concatenate(
            [
                labels[0].ravel(),
                labels[-1].ravel(),
                labels[:, 0].ravel(),
                labels[:, -1].ravel(),
                labels[:, :, 0].ravel(),
                labels[:, :, -1].ravel(),
            ]
        )
    )
    border = border[border > 0]
    outside = np.isin(labels, border) & empty
    return ~outside


def _largest_component(solid: np.ndarray) -> np.ndarray:
    """Keep only the largest 6-connected solid component."""
    labels, n = ndimage.label(solid)
    if n <= 1:
        return solid
    counts = np.bincount(labels.ravel())
    counts[0] = 0
    return labels == counts.argmax()
