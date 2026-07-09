import numpy as np
import pytest

from topopt.fem import FEModel, hex_stiffness


def test_ke_symmetric():
    ke = hex_stiffness()
    assert ke.shape == (24, 24)
    assert np.allclose(ke, ke.T, atol=1e-12)


def test_ke_rigid_body_modes():
    """A free element must have exactly 6 zero-energy (rigid) modes."""
    ke = hex_stiffness()
    eigvals = np.linalg.eigvalsh(ke)
    assert np.all(eigvals[:6] < 1e-10)
    assert np.all(eigvals[6:] > 1e-6)


def test_ke_translation_is_zero_force():
    ke = hex_stiffness()
    u = np.zeros(24)
    u[0::3] = 1.0  # rigid translation in x
    assert np.linalg.norm(ke @ u) < 1e-12


def test_cantilever_deflects_along_load():
    """A small cantilever fixed at one end, loaded at the other, must deflect
    in the load direction with finite displacement."""
    occ = np.ones((8, 2, 2), dtype=bool)
    fixed = np.zeros_like(occ)
    fixed[0] = True
    load = np.zeros_like(occ)
    load[-1] = True
    model = FEModel(occ, fixed, load, np.array([0.0, 0.0, -1.0]))
    scale = model.element_scale(np.ones(model.nel))
    u = model.solve(scale)
    tip_z = u[2::3]
    assert np.isfinite(u).all()
    assert tip_z.min() < 0  # moves downward
    c, ce = model.compliance(u, scale)
    assert c > 0
    assert (ce >= 0).all()


def test_fixed_dofs_stay_zero():
    occ = np.ones((4, 2, 2), dtype=bool)
    fixed = np.zeros_like(occ)
    fixed[0] = True
    load = np.zeros_like(occ)
    load[-1] = True
    model = FEModel(occ, fixed, load, np.array([0.0, 0.0, -1.0]))
    u = model.solve(model.element_scale(np.ones(model.nel)))
    assert np.abs(u[model.fixed_dofs]).max() == 0.0
