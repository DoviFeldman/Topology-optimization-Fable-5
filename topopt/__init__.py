"""topopt — SIMP topology optimization from STL to smooth printable STL.

Modules:
    voxelize  — STL loading, repair, solid voxelization
    fem       — 3D linear-elastic FEA on the voxel grid (sparse / matrix-free)
    simp      — SIMP compliance-minimization loop (density filter + OC update)
    meshing   — density field -> smooth watertight STL (marching cubes + Taubin)
    pipeline  — end-to-end orchestration with progress reporting
"""

__version__ = "1.0.0"
