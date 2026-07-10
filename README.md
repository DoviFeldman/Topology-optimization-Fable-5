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
| `--fix` | bottom | clamped face(s), comma-separated: `left,right` makes a bridge |
| `--load` | top | face that carries the load |
| `--dir` | -z | direction of the load (+x, -x, +y, -y, +z, -z) |
| `--load-extent` | 0.25 | fraction of the loaded face carrying force (small patch ⇒ dramatic branching structures; 1.0 = whole face) |
| `--load-cases` | 1 | 1, 3 or 5 force directions. One direction produces a single truss; several produce cross-braced internal webbing (N× slower) |
| `--shape-preserve` | 0.0 | 0–1 density floor on the input's surface shell. The silhouette survives where it carries load and opens where it doesn't; 0.5+ keeps the skin everywhere |
| `--expand` | 0.0 | 0–0.3: grow the buildable space outward, letting struts form outside the input shape |
| `--rmin` | 2.0 | smoothing filter radius in voxels (1.5–2.5 sensible) |
| `--max-iter` | 60 | max optimization iterations |

### Getting the "real topology-optimized print" look

Three failure modes, three cures:

- **Blob** — the force is spread out and too much material is kept.
  Concentrate it: `--load-extent 0.15 --volfrac 0.2`.
- **Single stick / everything vanished** — one force direction only needs one
  strut. Make it resist wobble: `--load-cases 3` (or 5), and protect the
  silhouette with `--shape-preserve 0.35`.
- **Chunky webs** — struts can't be thinner than a voxel: raise
  `--resolution` to 96+, lower `--rmin` to 1.4.

The showcase recipe (webby bridge that keeps its outline):

```bash
python optimize.py test_shapes/beam.stl bridge.stl \
    --fix left,right --load top --dir -z --load-extent 0.15 \
    --load-cases 3 --shape-preserve 0.35 --volfrac 0.22 --rmin 1.5
```

The web UI wraps all of this in scenario presets (bridge / bracket / tower /
hook), skeletal-to-solid style chips, webbing chips, and "keep outer shape" /
"extra build room" sliders — and draws the anchors and force arrow directly
on your model.

### macOS / Apple Silicon note

An M1/M2 Mac is typically 5–10× faster than a small cloud container for this
workload. On older macOS versions (e.g. Big Sur), plain `pip` may refuse
recent NumPy/SciPy wheels (they target newer macOS). The reliable path is
[Miniforge](https://github.com/conda-forge/miniforge) (arm64, supports
macOS ≥ 11):

```bash
conda create -n topopt python=3.12
conda activate topopt
conda install numpy scipy scikit-image trimesh shapely networkx pyamg
pip install fastapi 'uvicorn[standard]' python-multipart mapbox-earcut pytest
uvicorn main:app --port 8000
```

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
2. **Boundary conditions** — one or more faces are clamped (they can't move)
   and a load pushes on a patch of another face. The patch size is tunable:
   a concentrated patch produces distinct branching force paths, a whole-face
   load produces uniform slabs. Regions snap to the nearest occupied voxels
   of the chosen face, so parts with sloped or stepped faces work too.
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
