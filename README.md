# Topology Optimizer — STL in, smooth optimized STL out

Upload any solid STL, and this tool carves it into an organic, weight-optimized
shape using real SIMP topology optimization (compliance minimization with 3D
finite elements), then outputs a **smooth, watertight, 3D-printable STL** —
no voxel staircase, no broken meshes.

```
input STL ──> repair ──> solid voxelization ──> SIMP optimization (FEA loop)
          ──> 2x upsample + gaussian blur ──> marching cubes ──> Taubin smoothing
          ──> watertight repair ──> output STL (original scale restored)
```

**Screenshots:** *(placeholder — add screenshots of the web UI and example results here)*

## What you need

- Python **3.11 or newer** (`python3 --version` to check)
- Around **8 GB of RAM** for the default resolution (96). Less RAM? Use a
  lower resolution — 48 works on very small machines.
- No GPU, no compilers, no OS-specific anything. Everything installs with pip.

---

## Option A: Run it on your own computer (easiest)

Step by step — type each line into a terminal:

```bash
# 1. get the code
git clone https://github.com/DoviFeldman/Topology-optimization-Fable-5.git
cd Topology-optimization-Fable-5

# 2. make an isolated Python environment (keeps your system clean)
python3 -m venv .venv

# 3. activate it   (Windows: .venv\Scripts\activate)
source .venv/bin/activate

# 4. install the libraries
pip install -r requirements.txt

# 5. start the web app
uvicorn main:app --host 0.0.0.0 --port 8000
```

Now open **http://localhost:8000** in your browser:

1. Drag an STL file onto the drop zone (make test files with
   `python make_test_shapes.py` — they appear in `test_shapes/`).
2. (Optional) adjust the sliders — the defaults are good.
3. Click **Optimize** and watch the live progress bar.
4. When it finishes, orbit around the result in 3D and click
   **Download optimized STL**.

## Option B: Run it in GitHub Codespaces (nothing to install)

1. On the GitHub page of this repo click **Code → Codespaces →
   Create codespace on main**.
2. Wait for setup to finish (it installs everything automatically).
3. In the terminal at the bottom, run:
   `uvicorn main:app --host 0.0.0.0 --port 8000`
4. A "port 8000" notification pops up — click **Open in Browser**.

## Option C: Command line only (no browser)

```bash
python make_test_shapes.py          # creates test_shapes/*.stl
python optimize.py test_shapes/l_bracket.stl output.stl \
    --resolution 96 --volfrac 0.35 --fix bottom --load top --dir -z
```

Progress prints to the terminal. All flags are optional; run
`python optimize.py --help` for the full list:

| flag | default | meaning |
|---|---|---|
| `--resolution` | 96 | voxels along the longest axis (24–160). Higher = finer + slower |
| `--volfrac` | 0.35 | fraction of material to keep (0.1–0.8) |
| `--fix` | bottom | face that is clamped (bottom/top/left/right/front/back) |
| `--load` | top | face that carries the load |
| `--dir` | -z | direction of the load (+x, -x, +y, -y, +z, -z) |
| `--rmin` | 2.0 | smoothing filter radius in voxels (1.5–2.5 sensible) |
| `--max-iter` | 60 | max optimization iterations |

## Option D: Deploy on a VPS

See **[deploy.md](deploy.md)** — full beginner-friendly walkthrough
(systemd service, optional nginx, and a Docker alternative).

---

## How it works (short version)

1. **Voxelize** — the STL surface is point-sampled into a voxel shell, then an
   outside flood-fill recovers the solid interior. This is robust to messy,
   non-manifold, even leaky meshes, and handles 1–2M-triangle files in bounded
   memory. The input mesh is discarded afterwards — the rest of the pipeline
   only sees voxels.
2. **Boundary conditions** — by default the bottom layer of voxels is clamped
   and a distributed downward load is applied to the top layer. Both faces and
   the load direction are configurable; regions snap to the nearest occupied
   voxels of the chosen face.
3. **SIMP optimization** — classic top3d-style compliance minimization:
   8-node hexahedral elements, penalization p=3, cone density filter, and
   Optimality Criteria updates, for up to 60 iterations or until the design
   stops changing. Only occupied voxels get degrees of freedom. The linear
   systems are solved with direct sparse LU (small models), AMG-preconditioned
   conjugate gradients (medium), or a matrix-free CG (large models).
4. **Smooth extraction** — the final *continuous* density field is upsampled
   2x, lightly blurred, contoured with marching cubes at the 0.5 level, and
   Taubin-smoothed (no shrinkage). Largest component kept, holes filled,
   watertightness verified, original scale restored, binary STL written.

Structural accuracy is intentionally approximate — this is a tool for making
strong-*looking*, beautiful, printable parts, not certified engineering.

## Project layout

```
topopt/voxelize.py   STL load, repair, solid voxelization
topopt/fem.py        3D FEA on the voxel grid (sparse + matrix-free solvers)
topopt/simp.py       SIMP loop: density filter + optimality criteria
topopt/meshing.py    density field -> smooth watertight mesh
topopt/pipeline.py   end-to-end orchestration + progress reporting
main.py              FastAPI backend (job queue, REST API, serves the UI)
optimize.py          command-line interface
static/index.html    the whole frontend (Three.js viewer, no build step)
make_test_shapes.py  generates test STLs (box, box with hole, L-bracket)
tests/               pytest suite (fast tests + slow acceptance tests)
```

## API (if you want to script against it)

| endpoint | method | purpose |
|---|---|---|
| `/api/jobs` | POST | multipart upload: `file` + form fields (`resolution`, `volfrac`, `fix_face`, `load_face`, `load_dir`, …) → `{id: ...}` |
| `/api/jobs/{id}` | GET | status: phase, iteration, compliance, queue position |
| `/api/jobs/{id}/result` | GET | download the optimized STL |
| `/api/jobs/{id}/preview` | GET | same STL, served for the 3D viewer |

One optimization runs at a time; extra jobs wait in a queue (single-user tool,
in-memory job store).

## Measured performance

Numbers from a deliberately *tiny* machine (1 CPU core, 1 GB RAM VPS) — any
normal laptop or Codespace is several times faster:

| run | time | peak RAM |
|---|---|---|
| L-bracket, resolution 32, 10 iterations | ~70 s | ~0.4 GB |
| L-bracket, resolution 48, 30 iterations | ~250 s | ~0.6 GB |
| Voxelize an 851k-triangle STL at resolution 96 | ~7 s | ~0.4 GB |

The default resolution 96 is sized for a 4-core / 8 GB machine (e.g. a free
GitHub Codespace). On machines with 2 GB RAM or less, stay at resolution 48
or below.

Output quality at resolution 48: watertight, single body, mean angle between
adjacent faces 2.5 degrees (a raw voxel mesh would be ~90) — i.e. no visible
staircase.

## Running the tests

```bash
pytest                # fast suite: unit tests + 3 end-to-end runs at res 32
pytest -m slow        # acceptance tests: full-resolution L-bracket run,
                      # 1M+ triangle voxelization (needs ~8 GB RAM)
```

Note: the fast suite runs three full optimizations, so it wants ~2 GB of free
RAM; on smaller machines run the unit tests alone
(`pytest tests/test_fem.py tests/test_voxelize.py`).
