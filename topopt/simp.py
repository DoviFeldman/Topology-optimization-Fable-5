"""SIMP topology optimization: compliance minimization on the voxel grid.

Standard 3D SIMP (in the spirit of top3d): penalized density interpolation
(p = 3), a cone-kernel density filter against checkerboarding, and an
Optimality Criteria update with a move limit. The density filter is evaluated
as a normalized convolution over the full grid (cheap and simple), and the
volume constraint is met via bisection on the unfiltered densities plus a
small integral-feedback correction for the filter's slight non-volume-
preservation near boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from scipy import ndimage

from .fem import FEModel


ProgressFn = Callable[[int, int, float, float], None]
"""(iteration, max_iterations, compliance, density_change) -> None"""


@dataclass
class SimpResult:
    density_grid: np.ndarray  # (nx, ny, nz) float32, xPhys embedded in grid
    compliance_history: list[float] = field(default_factory=list)
    iterations: int = 0
    converged: bool = False
    volume_fraction: float = 0.0


def _cone_kernel(rmin: float) -> np.ndarray:
    """Linear (cone) filter weights w = max(0, rmin - dist) on a cube stencil."""
    r = int(np.ceil(rmin - 1e-9))
    axis = np.arange(-r, r + 1, dtype=np.float64)
    dx, dy, dz = np.meshgrid(axis, axis, axis, indexing="ij")
    dist = np.sqrt(dx * dx + dy * dy + dz * dz)
    return np.maximum(0.0, rmin - dist)


class DensityFilter:
    """Normalized cone-kernel convolution restricted to the occupied domain."""

    def __init__(self, occ: np.ndarray, rmin: float) -> None:
        self.occ = occ
        self.kernel = _cone_kernel(rmin)
        self.hs = ndimage.convolve(
            occ.astype(np.float64), self.kernel, mode="constant"
        )
        self.hs[~occ] = 1.0  # avoid divide-by-zero off-domain
        np.maximum(self.hs, 1e-12, out=self.hs)

    def _conv(self, grid: np.ndarray) -> np.ndarray:
        return ndimage.convolve(grid, self.kernel, mode="constant")

    def forward(self, x: np.ndarray) -> np.ndarray:
        """xPhys = (H x) / Hs, both given as element vectors."""
        grid = np.zeros(self.occ.shape, dtype=np.float64)
        grid[self.occ] = x
        return (self._conv(grid) / self.hs)[self.occ]

    def backward(self, sens: np.ndarray) -> np.ndarray:
        """Chain rule of the density filter: H (sens / Hs)."""
        grid = np.zeros(self.occ.shape, dtype=np.float64)
        grid[self.occ] = sens
        grid /= self.hs
        grid[~self.occ] = 0.0
        return self._conv(grid)[self.occ]


def simp_optimize(
    occ: np.ndarray,
    fixed_vox: np.ndarray,
    load_vox: np.ndarray,
    load_dir: np.ndarray,
    volfrac: float = 0.35,
    rmin: float = 2.0,
    penal: float = 3.0,
    max_iter: int = 60,
    change_tol: float = 0.01,
    move: float = 0.2,
    progress: ProgressFn | None = None,
) -> SimpResult:
    """Run the SIMP loop and return the final physical density field.

    Voxels within one cell of the load/support regions are kept passively
    solid so the boundary conditions always stay attached to material.
    """
    if not 0.05 <= volfrac <= 0.9:
        raise ValueError("volfrac must be within 0.05..0.9")
    fem = FEModel(occ, fixed_vox, load_vox, load_dir)
    filt = DensityFilter(occ, rmin)

    passive_grid = ndimage.binary_dilation(fixed_vox | load_vox, iterations=1) & occ
    passive = passive_grid[occ]

    nel = fem.nel
    x = np.full(nel, volfrac)
    x[passive] = 1.0
    x_phys = filt.forward(x)
    x_phys[passive] = 1.0

    dv = filt.backward(np.ones(nel))  # constant across iterations

    result = SimpResult(density_grid=np.zeros(occ.shape, dtype=np.float32))
    vol_target = volfrac  # adjusted by feedback so filtered volume hits volfrac
    change = 1.0

    for it in range(1, max_iter + 1):
        # inexact solves while the design is still moving fast, tight near
        # convergence — the OC update is robust to small errors in u
        fem.cg_rtol = 5e-4 if change > 0.15 else (2e-4 if change > 0.05 else 1e-4)
        scale = fem.element_scale(x_phys, penal)
        u = fem.solve(scale)
        c, ce = fem.compliance(u, scale)
        result.compliance_history.append(c)

        dc = -penal * np.clip(x_phys, 0.0, 1.0) ** (penal - 1.0) * (1.0 - 1e-4) * ce
        dc = filt.backward(dc)
        np.minimum(dc, -1e-30, out=dc)

        x_new = _oc_update(x, dc, dv, vol_target, move, passive)
        x_phys = filt.forward(x_new)
        x_phys[passive] = 1.0
        np.clip(x_phys, 0.0, 1.0, out=x_phys)

        # feedback: nudge the bisection target so achieved volume == volfrac
        achieved = float(x_phys.mean())
        vol_target = float(
            np.clip(vol_target + 0.6 * (volfrac - achieved), 0.5 * volfrac, 1.2 * volfrac)
        )

        change = float(np.max(np.abs(x_new - x)))
        x = x_new
        result.iterations = it
        result.volume_fraction = achieved
        if progress is not None:
            progress(it, max_iter, c, change)
        if change < change_tol:
            result.converged = True
            break

    result.density_grid[occ] = x_phys.astype(np.float32)
    return result


def _oc_update(
    x: np.ndarray,
    dc: np.ndarray,
    dv: np.ndarray,
    vol_target: float,
    move: float,
    passive: np.ndarray,
) -> np.ndarray:
    """Optimality Criteria update with bisection on the Lagrange multiplier."""
    l1, l2 = 0.0, 1e9
    lower = np.maximum(x - move, 0.0)
    upper = np.minimum(x + move, 1.0)
    x_new = x
    while (l2 - l1) / max(l2 + l1, 1e-30) > 1e-4:
        lmid = 0.5 * (l1 + l2)
        x_new = np.clip(x * np.sqrt(-dc / dv / lmid), lower, upper)
        x_new[passive] = 1.0
        if x_new.mean() > vol_target:
            l1 = lmid
        else:
            l2 = lmid
    return x_new
