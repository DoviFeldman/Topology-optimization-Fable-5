import numpy as np
import trimesh

from topopt.voxelize import load_mesh, voxelize_solid


def test_box_fills_solid(shape_dir):
    mesh = load_mesh(str(shape_dir / "box.stl"))
    grid = voxelize_solid(mesh, resolution=48)
    # a 60x40x30 box: occupied fraction of its own bbox should be ~100%
    extents_vox = np.ceil(np.array([60, 40, 30]) / grid.pitch)
    expected = extents_vox.prod()
    assert grid.n_solid > 0.85 * expected
    # interior voxel (grid center) must be solid — proves it's a fill, not a shell
    cx, cy, cz = (np.array(grid.occ.shape) // 2).tolist()
    assert grid.occ[cx, cy, cz]


def test_hole_stays_open(shape_dir):
    mesh = load_mesh(str(shape_dir / "box_with_hole.stl"))
    grid = voxelize_solid(mesh, resolution=64)
    # voxel at the center of the through-hole must be empty
    center_world = np.array([0.0, 0.0, 12.5])
    idx = np.floor((center_world - grid.origin) / grid.pitch).astype(int)
    assert not grid.occ[idx[0], idx[1], idx[2]]
    # but the plate material must be solid
    plate_world = np.array([25.0, 25.0, 12.5])
    idx = np.floor((plate_world - grid.origin) / grid.pitch).astype(int)
    assert grid.occ[idx[0], idx[1], idx[2]]


def test_leaky_mesh_repaired_and_fills(tmp_path):
    """A box missing one triangle must be repaired on load and fill solid."""
    box = trimesh.creation.box(extents=[10, 10, 10])
    leaky = trimesh.Trimesh(
        vertices=box.vertices.copy(), faces=box.faces[1:].copy(), process=False
    )
    assert not leaky.is_watertight
    path = tmp_path / "leaky.stl"
    leaky.export(str(path))
    mesh = load_mesh(str(path))  # load_mesh attempts trimesh repair
    grid = voxelize_solid(mesh, resolution=32)
    interior_fraction = grid.n_solid / (32**3)
    assert interior_fraction > 0.5


def test_pinhole_mesh_fills_without_repair():
    """A tiny pinhole (small missing triangle) must be closed by the
    morphological fallback even when mesh repair is skipped entirely."""
    box = trimesh.creation.box(extents=[10, 10, 10])
    for _ in range(3):  # shrink triangles so the hole is a few voxels wide
        box = box.subdivide()
    pinhole = trimesh.Trimesh(
        vertices=box.vertices.copy(), faces=box.faces[1:].copy(), process=False
    )
    assert not pinhole.is_watertight
    grid = voxelize_solid(pinhole, resolution=32)
    interior_fraction = grid.n_solid / (32**3)
    assert interior_fraction > 0.5


def test_world_transform_roundtrip(shape_dir):
    mesh = load_mesh(str(shape_dir / "box.stl"))
    grid = voxelize_solid(mesh, resolution=48)
    # voxel-center cloud must land inside (a hair beyond) the original bounds
    centers = np.argwhere(grid.occ) + 0.5
    world = grid.to_world(centers)
    assert (world.min(axis=0) >= mesh.bounds[0] - 2 * grid.pitch).all()
    assert (world.max(axis=0) <= mesh.bounds[1] + 2 * grid.pitch).all()
