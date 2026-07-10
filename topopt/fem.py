"""3D linear-elastic finite element analysis on a voxel grid.

Only occupied voxels get elements/DOFs — the empty part of the bounding box is
never assembled, which keeps memory proportional to the actual part volume.

Three solver strategies, picked automatically by problem size:
  * small   (<= SPLU_MAX_DOF free DOFs): direct sparse LU (exact, fast at small scale)
  * medium  (assembly fits in memory):    CG preconditioned with AMG (pyamg) if
                                          available, otherwise Jacobi
  * large   (assembly would be too big):  matrix-free element-by-element CG with
                                          Jacobi preconditioning

Elements are unit cubes (the grid pitch is factored out; compliance scale is
arbitrary, which is fine for topology optimization). Young's modulus of solid
material is 1.0; void material keeps E_MIN so the system is never singular even
when the density field disconnects — important for iterative-solver robustness.
"""

from __future__ import annotations

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import splu

try:  # optional accelerator — graceful fallback to Jacobi if missing
    import pyamg

    HAVE_PYAMG = True
except ImportError:  # pragma: no cover
    HAVE_PYAMG = False

# Void-to-solid stiffness ratio. 1e-4 (rather than the textbook 1e-9) keeps the
# system well-conditioned for CG while having no visible effect on the result.
E_MIN = 1e-4

# Direct LU only for truly tiny systems: 3D elasticity fill-in makes splu
# far slower than AMG-preconditioned CG already at ~20k DOFs (measured 8.2s
# factor vs 0.7s AMG solve on a res-32 L-bracket).
SPLU_MAX_DOF = 5_000
ASSEMBLE_MAX_ENTRIES = 150e6   # assemble sparse K below this many COO entries

# Local node ordering of the hexahedron: offsets from the element's (i,j,k)
# corner. DOF d of local node n is local DOF 3*n + d (d: 0=x, 1=y, 2=z).
NODE_OFFSETS = np.array(
    [
        [0, 0, 0],
        [1, 0, 0],
        [1, 1, 0],
        [0, 1, 0],
        [0, 0, 1],
        [1, 0, 1],
        [1, 1, 1],
        [0, 1, 1],
    ],
    dtype=np.int64,
)


def hex_stiffness(nu: float = 0.3) -> np.ndarray:
    """24x24 stiffness matrix of a unit-cube 8-node hexahedron (E=1).

    Computed by standard isoparametric 2x2x2 Gauss quadrature, so it is exact
    for this element (integrand is polynomial of adequate order).
    """
    D = (
        1.0
        / ((1 + nu) * (1 - 2 * nu))
        * np.array(
            [
                [1 - nu, nu, nu, 0, 0, 0],
                [nu, 1 - nu, nu, 0, 0, 0],
                [nu, nu, 1 - nu, 0, 0, 0],
                [0, 0, 0, (1 - 2 * nu) / 2, 0, 0],
                [0, 0, 0, 0, (1 - 2 * nu) / 2, 0],
                [0, 0, 0, 0, 0, (1 - 2 * nu) / 2],
            ]
        )
    )
    corners = NODE_OFFSETS.astype(float)
    signs = 2.0 * corners - 1.0  # node signs in natural coords (-1/+1)
    g = 1.0 / np.sqrt(3.0)
    ke = np.zeros((24, 24))
    for gx in (-g, g):
        for gy in (-g, g):
            for gz in (-g, g):
                xi = np.array([gx, gy, gz])
                # dN/dxi for the trilinear shape functions
                dn = np.empty((8, 3))
                for i in range(8):
                    t = (1.0 + signs[i] * xi) / 2.0
                    dn[i, 0] = signs[i, 0] / 2.0 * t[1] * t[2]
                    dn[i, 1] = signs[i, 1] / 2.0 * t[0] * t[2]
                    dn[i, 2] = signs[i, 2] / 2.0 * t[0] * t[1]
                jac = corners.T @ dn  # dx/dxi (0.5*I for the unit cube)
                dnx = dn @ np.linalg.inv(jac)  # dN/dx
                b = np.zeros((6, 24))
                for i in range(8):
                    c = 3 * i
                    b[0, c] = dnx[i, 0]
                    b[1, c + 1] = dnx[i, 1]
                    b[2, c + 2] = dnx[i, 2]
                    b[3, c] = dnx[i, 1]
                    b[3, c + 1] = dnx[i, 0]
                    b[4, c + 1] = dnx[i, 2]
                    b[4, c + 2] = dnx[i, 1]
                    b[5, c] = dnx[i, 2]
                    b[5, c + 2] = dnx[i, 0]
                ke += b.T @ D @ b * np.linalg.det(jac)
    return ke


class FEModel:
    """FE model over the occupied voxels of a grid.

    Parameters
    ----------
    occ : (nx, ny, nz) bool array — solid voxels (each becomes one element)
    fixed_vox : bool array, same shape — voxels whose nodes are fully clamped
    load_vox : bool array, same shape — voxels whose nodes carry the load
    load_dir : (3,) or (ncases, 3) float — force direction(s). Multiple rows
        define independent load cases solved against the same stiffness matrix;
        compliance is their (weighted) sum. Multi-case designs must resist
        every direction at once, which is what produces cross-braced webbing
        instead of a single strut path.
    case_weights : optional (ncases,) float — weight of each case's compliance
    nu : Poisson ratio
    """

    def __init__(
        self,
        occ: np.ndarray,
        fixed_vox: np.ndarray,
        load_vox: np.ndarray,
        load_dir: np.ndarray,
        case_weights: np.ndarray | None = None,
        nu: float = 0.3,
    ) -> None:
        if not occ.any():
            raise ValueError("empty occupancy grid")
        self.occ = occ
        self.ke = hex_stiffness(nu)

        # ---- element -> global DOF map (only nodes touched by solid voxels)
        eijk = np.argwhere(occ)  # (nel, 3), C-order == np.flatnonzero order
        self.nel = len(eijk)
        corners = eijk[:, None, :] + NODE_OFFSETS[None, :, :]  # (nel, 8, 3)
        node_grid = np.full(
            (occ.shape[0] + 1, occ.shape[1] + 1, occ.shape[2] + 1), -1, dtype=np.int64
        )
        cx, cy, cz = corners[..., 0], corners[..., 1], corners[..., 2]
        node_grid[cx, cy, cz] = 0
        used = np.argwhere(node_grid == 0)
        node_grid[used[:, 0], used[:, 1], used[:, 2]] = np.arange(len(used))
        self.node_coords = used.astype(np.float64)  # (nnode, 3) grid coords
        self.nnode = len(used)
        self.ndof = 3 * self.nnode
        enode = node_grid[cx, cy, cz]  # (nel, 8)
        self.edof = (3 * enode[:, :, None] + np.arange(3)[None, None, :]).reshape(
            self.nel, 24
        ).astype(np.int32)

        # ---- boundary conditions
        self.fixed_dofs = self._voxel_dofs(node_grid, fixed_vox)
        load_dofs_all = self._voxel_dofs(node_grid, load_vox)
        load_nodes = np.unique(load_dofs_all // 3)
        if len(load_nodes) == 0 or len(self.fixed_dofs) == 0:
            raise ValueError("empty load or support region")
        dirs = np.atleast_2d(np.asarray(load_dir, dtype=float))
        self.ncases = len(dirs)
        self.single_case = np.asarray(load_dir).ndim == 1
        self.case_weights = (
            np.ones(self.ncases) if case_weights is None else np.asarray(case_weights, float)
        )
        self.f = np.zeros((self.ncases, self.ndof))
        for ci, d in enumerate(dirs):
            d = d / np.linalg.norm(d)
            for axis in range(3):
                if d[axis] != 0.0:
                    self.f[ci, 3 * load_nodes + axis] = d[axis] / len(load_nodes)
        self.free_mask = np.ones(self.ndof, dtype=bool)
        self.free_mask[self.fixed_dofs] = False
        self.f[:, ~self.free_mask] = 0.0
        self.n_free = int(self.free_mask.sum())

        # ---- solver strategy
        entries = self.nel * 576
        if self.n_free <= SPLU_MAX_DOF:
            self.strategy = "splu"
        elif entries <= ASSEMBLE_MAX_ENTRIES:
            self.strategy = "amg-cg" if HAVE_PYAMG else "jacobi-cg"
        else:
            self.strategy = "matrix-free-cg"

        self._u = np.zeros((self.ncases, self.ndof))  # warm starts across solves
        self._assembly_cache: tuple | None = None
        self._amg = None
        self._solve_count = 0
        self.amg_rebuild_every = 8
        self.cg_rtol = 1e-4
        self.cg_maxiter = 800
        self.last_cg_iters = 0

    @staticmethod
    def _voxel_dofs(node_grid: np.ndarray, vox: np.ndarray) -> np.ndarray:
        """All 3 DOFs of every corner node of the given voxels."""
        vijk = np.argwhere(vox)
        if len(vijk) == 0:
            return np.zeros(0, dtype=np.int64)
        corners = vijk[:, None, :] + NODE_OFFSETS[None, :, :]
        nodes = node_grid[corners[..., 0], corners[..., 1], corners[..., 2]]
        nodes = np.unique(nodes[nodes >= 0])
        return (3 * nodes[:, None] + np.arange(3)[None, :]).ravel()

    # ------------------------------------------------------------------ #

    def element_scale(self, x_phys: np.ndarray, penal: float = 3.0) -> np.ndarray:
        """SIMP stiffness interpolation E(x) = E_min + x^p (1 - E_min)."""
        return E_MIN + np.clip(x_phys, 0.0, 1.0) ** penal * (1.0 - E_MIN)

    def solve(self, scale: np.ndarray) -> np.ndarray:
        """Solve K(scale) u = f for every load case.

        Returns (ncases, ndof), or a flat (ndof,) vector when the model was
        built with a single (3,) direction — the pre-multi-case API.
        """
        self._solve_count += 1
        if self.strategy == "splu":
            u = self._solve_direct(scale)
        else:
            u = self._solve_cg(scale)
        self._u = u
        return u[0] if self.single_case else u

    def compliance(self, u: np.ndarray, scale: np.ndarray) -> tuple[float, np.ndarray]:
        """Weighted-total compliance and per-element strain energy over all cases."""
        u2 = np.atleast_2d(u)
        ce = np.zeros(self.nel)
        for ci in range(u2.shape[0]):
            ue = u2[ci][self.edof]
            ce_i = np.einsum("ij,ij->i", ue @ self.ke, ue)
            ce += self.case_weights[ci] * ce_i
        np.maximum(ce, 0.0, out=ce)  # clip tiny negative round-off
        return float(np.dot(scale, ce)), ce

    # ---- direct ------------------------------------------------------- #

    def _reduced_indices(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """COO row/col indices of the free-DOF submatrix, cached."""
        if self._assembly_cache is None:
            red = np.full(self.ndof, -1, dtype=np.int32)
            red[self.free_mask] = np.arange(self.n_free, dtype=np.int32)
            rows = np.broadcast_to(self.edof[:, :, None], (self.nel, 24, 24))
            cols = np.broadcast_to(self.edof[:, None, :], (self.nel, 24, 24))
            rows = red[rows.reshape(-1)]
            cols = red[cols.reshape(-1)]
            valid = (rows >= 0) & (cols >= 0)
            self._assembly_cache = (rows[valid], cols[valid], valid)
        return self._assembly_cache

    def _assemble(self, scale: np.ndarray) -> sparse.csr_matrix:
        rows, cols, valid = self._reduced_indices()
        data = (scale[:, None] * self.ke.reshape(1, 576)).reshape(-1)[valid]
        k = sparse.coo_matrix(
            (data, (rows, cols)), shape=(self.n_free, self.n_free)
        ).tocsr()
        return k

    def _solve_direct(self, scale: np.ndarray) -> np.ndarray:
        lu = splu(self._assemble(scale).tocsc())  # one factorization, all cases
        u = np.zeros((self.ncases, self.ndof))
        for ci in range(self.ncases):
            u[ci, self.free_mask] = lu.solve(self.f[ci, self.free_mask])
        self.last_cg_iters = 0
        return u

    # ---- iterative ----------------------------------------------------- #

    def _rigid_modes(self) -> np.ndarray:
        """Near-nullspace (6 rigid-body modes) for AMG, on free DOFs."""
        xyz = self.node_coords - self.node_coords.mean(axis=0)
        b = np.zeros((self.ndof, 6))
        for a in range(3):
            b[a::3, a] = 1.0
        x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        b[0::3, 3], b[1::3, 3] = -y, x  # rotation about z
        b[1::3, 4], b[2::3, 4] = -z, y  # rotation about x
        b[0::3, 5], b[2::3, 5] = z, -x  # rotation about y
        return b[self.free_mask]

    def _jacobi_diag(self, scale: np.ndarray) -> np.ndarray:
        ked = self.ke.diagonal()
        diag = np.bincount(
            self.edof.ravel(),
            weights=(scale[:, None] * ked[None, :]).ravel(),
            minlength=self.ndof,
        )
        diag[~self.free_mask] = 1.0
        return diag

    def _matvec_free(self, scale: np.ndarray):
        """Matrix-free K@u operator with fixed DOFs pinned to zero."""
        edof, ke, ndof, free = self.edof, self.ke, self.ndof, self.free_mask

        def op(u: np.ndarray) -> np.ndarray:
            up = np.where(free, u, 0.0)
            fe = (up[edof] @ ke) * scale[:, None]
            out = np.bincount(edof.ravel(), weights=fe.ravel(), minlength=ndof)
            out[~free] = 0.0
            return out

        return op

    def _solve_cg(self, scale: np.ndarray) -> np.ndarray:
        u = np.zeros((self.ncases, self.ndof))
        if self.strategy == "matrix-free-cg":
            op = self._matvec_free(scale)
            inv_diag = 1.0 / self._jacobi_diag(scale)

            def precond(r: np.ndarray) -> np.ndarray:
                return r * inv_diag

            for ci in range(self.ncases):
                u[ci] = self._pcg(op, self.f[ci], self._u[ci], precond)
            return u

        # assembly and preconditioner are shared by all load cases — extra
        # cases only cost extra CG solves, not extra setup
        k = self._assemble(scale)
        if self.strategy == "amg-cg" and (
            self._amg is None or self._solve_count % self.amg_rebuild_every == 1
        ):
            # A slightly stale AMG hierarchy still preconditions well, so we
            # only rebuild it every few design iterations.
            self._amg = pyamg.smoothed_aggregation_solver(
                k, B=self._rigid_modes(), smooth="jacobi", max_coarse=300
            ).aspreconditioner(cycle="V")
        if self.strategy == "amg-cg":
            precond_red = self._amg.matvec
        else:
            inv_diag_red = 1.0 / k.diagonal()

            def precond_red(r: np.ndarray) -> np.ndarray:
                return r * inv_diag_red

        for ci in range(self.ncases):
            u_red = self._pcg(
                lambda v: k @ v, self.f[ci, self.free_mask],
                self._u[ci, self.free_mask], precond_red,
            )
            u[ci, self.free_mask] = u_red
        return u

    def _pcg(self, op, b: np.ndarray, x0: np.ndarray, precond) -> np.ndarray:
        """Preconditioned conjugate gradients with warm start and an iteration
        cap. An inexact solve is acceptable inside the SIMP loop — the design
        update is robust to small errors in u, and warm starts make later
        solves nearly exact anyway."""
        x = x0.copy()
        r = b - op(x)
        b_norm = np.linalg.norm(b)
        if b_norm == 0.0:
            return np.zeros_like(b)
        z = precond(r)
        p = z.copy()
        rz = float(r @ z)
        it = 0
        for it in range(1, self.cg_maxiter + 1):
            ap = op(p)
            pap = float(p @ ap)
            if pap <= 0.0:  # numerical breakdown — bail with best iterate
                break
            alpha = rz / pap
            x += alpha * p
            r -= alpha * ap
            if np.linalg.norm(r) <= self.cg_rtol * b_norm:
                break
            z = precond(r)
            rz_new = float(r @ z)
            p = z + (rz_new / rz) * p
            rz = rz_new
        self.last_cg_iters = it
        return x
