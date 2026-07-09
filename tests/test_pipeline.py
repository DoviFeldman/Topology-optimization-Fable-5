import numpy as np
import pytest
import trimesh

from topopt.pipeline import Params, face_voxels, run_pipeline
from topopt.voxelize import load_mesh, voxelize_solid

FAST = Params(resolution=32, max_iter=10, taubin_iterations=15)
SHAPES = ["box.stl", "box_with_hole.stl", "l_bracket.stl"]


@pytest.fixture(scope="module")
def optimized(shape_dir, tmp_path_factory):
    """Run the full pipeline once per shape; all tests share the results."""
    out_dir = tmp_path_factory.mktemp("optimized")
    results = {}
    for name in SHAPES:
        out = out_dir / f"opt_{name}"
        result = run_pipeline(str(shape_dir / name), str(out), FAST)
        results[name] = (result, out)
    return results


@pytest.mark.parametrize("name", SHAPES)
def test_end_to_end_watertight(optimized, name):
    result, out = optimized[name]
    assert out.exists()
    reloaded = trimesh.load(str(out))
    assert reloaded.is_watertight
    assert result.watertight
    assert result.taubin_iterations > 0  # smoothing actually ran
    assert result.iterations >= 1


def test_material_was_removed(optimized, shape_dir):
    _, out = optimized["box.stl"]
    original = trimesh.load(str(shape_dir / "box.stl"))
    result = trimesh.load(str(out))
    assert result.volume < 0.8 * original.volume
    assert result.volume > 0.05 * original.volume


def test_scale_is_preserved(optimized, shape_dir):
    _, out = optimized["l_bracket.stl"]
    original = trimesh.load(str(shape_dir / "l_bracket.stl"))
    result = trimesh.load(str(out))
    orig_size = original.extents.max()
    opt_size = result.extents.max()
    assert 0.8 * orig_size <= opt_size <= 1.05 * orig_size


def test_no_staircase(optimized):
    """Smoothness: adjacent faces must meet at shallow angles, and few faces
    may be exactly axis-aligned (a raw voxel mesh is 100% axis-aligned with
    90-degree steps)."""
    _, out = optimized["l_bracket.stl"]
    mesh = trimesh.load(str(out))
    n = mesh.face_normals
    pairs = mesh.face_adjacency
    dots = np.einsum("ij,ij->i", n[pairs[:, 0]], n[pairs[:, 1]]).clip(-1, 1)
    mean_angle = np.degrees(np.arccos(dots)).mean()
    assert mean_angle < 15.0
    axis_aligned = (np.abs(n) > np.cos(np.radians(2.0))).any(axis=1)
    assert axis_aligned.mean() < 0.5


def test_face_voxels_snap():
    """On an L-shaped grid, 'top' must select the top of the tall leg only."""
    occ = np.zeros((10, 4, 10), dtype=bool)
    occ[:, :, :3] = True  # horizontal leg
    occ[:3, :, :] = True  # vertical leg
    top = face_voxels(occ, "top")
    assert top.any()
    xs, _, zs = np.nonzero(top)
    assert zs.min() >= 8  # near the top of the grid
    assert xs.max() <= 3  # only above the vertical leg
    bottom = face_voxels(occ, "bottom")
    assert bottom.sum() >= occ[:, :, 0].sum()  # whole underside


def test_params_validation():
    with pytest.raises(ValueError):
        Params(resolution=8).validate()
    with pytest.raises(ValueError):
        Params(volfrac=0.95).validate()
    with pytest.raises(ValueError):
        Params(fix_face="top", load_face="top").validate()
    with pytest.raises(ValueError):
        Params(load_dir="down").validate()
    Params().validate()  # defaults are always valid


@pytest.mark.slow
def test_big_input_voxelizes(tmp_path):
    """A >1M-triangle input must voxelize without blowing up memory."""
    import make_test_shapes as shapes

    big = shapes.make_big(shapes.make_box_with_hole())
    assert len(big.faces) > 1_000_000
    path = tmp_path / "big.stl"
    big.export(str(path))
    mesh = load_mesh(str(path))
    grid = voxelize_solid(mesh, resolution=96)
    assert grid.n_solid > 1000


@pytest.mark.slow
def test_acceptance_l_bracket_full(shape_dir, tmp_path):
    """Acceptance: L-bracket at resolution 96, volfrac 0.35, < 10 min."""
    import time

    t0 = time.time()
    out = tmp_path / "l_bracket_full.stl"
    result = run_pipeline(str(shape_dir / "l_bracket.stl"), str(out), Params())
    elapsed = time.time() - t0
    assert trimesh.load(str(out)).is_watertight
    assert result.taubin_iterations > 0
    assert elapsed < 600, f"took {elapsed:.0f}s (limit 600s)"
