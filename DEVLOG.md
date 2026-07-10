# Development Log — Topology Optimizer debugging & upgrade sessions

A blow-by-blow record of the live debugging/upgrade sessions between DoviFeldman
(first time testing the app) and Claude (Claude Code). User prompts are quoted
**verbatim**; the assistant's answers are condensed to their key points — the
originals were much longer. Fixes landed as commits on `feature/webbing-and-shape`
and `feature/paint-bc`.

---

## Round 1 — "the port doesn't work"

> hi the port doesnt work it says this page isnt working, can you fix it(im gonna
> ask you for a lot of fixes as we go through this this is my first time testing
> it dont go easy im excited)

**Diagnosis:** Nothing was broken — nothing was *running*. The devcontainer
forwards port 8000 automatically, but no process ever starts the server.

**Fix:** Started `uvicorn main:app --host 0.0.0.0 --port 8000`, verified HTTP 200,
pointed to the Ports tab → Open in Browser. Advised starting with
`test_shapes/l_bracket.stl` and dropping resolution to 48–64 on the 2-core
Codespace.

---

## Round 2 — the upload that wasn't JSON

> Upload failed: Unexpected token '<', "<html> <h"... it says this, the stl
> uploaded fine and i can see it in the screen and move it and everything only
> when i press optemize

**Diagnosis:** The 3D preview is local (no network), so "uploading" only happens
on Optimize. The POST never reached the app — GitHub Codespaces' port-forwarding
proxy rejects request bodies over **~16 MiB** with an nginx-style
`413 Request Entity Too Large` HTML page (measured: 16 MB passes, 20 MB fails),
and the frontend tried to parse that HTML as JSON.

**Fixes:**
- **Chunked uploads:** files over 8 MB are sliced into 8 MB pieces in the
  browser (`POST /api/uploads` to open a session, `POST /api/uploads/{id}/chunks`
  to append); `/api/jobs` accepts the reassembled upload's id. Live upload
  percentage in the status line.
- Readable error messages when a non-JSON error page comes back.

**Verified:** a 65 MB, 1.3M-triangle STL pushed through the real public proxy in
8 chunks, reassembled, and optimized to completion.

---

## Round 3 — "it froze" (it didn't)

> hi it froze

**Diagnosis:** The job was running fine (iteration 23/60, ~57 s/iteration ≈ 1
hour on 2 cores). But page refreshes had wiped the UI's memory, the user
re-clicked Optimize, and **three identical hour-long jobs** were queued — the
progress bar was watching the newest one, stuck at "#2 in queue, 0%". Slow +
watching the wrong job = looks frozen.

**Fixes:**
- **Cancel button** + `DELETE /api/jobs/{id}` (queued jobs die instantly; a
  running one stops at its next iteration via an exception raised in the
  progress callback).
- **Reattach after reload:** `GET /api/jobs` lists jobs; on page load the UI
  latches onto whatever is still running instead of letting duplicates pile up.
- **ETA display** (`iter 23/60 · ~35 min left`).
- **Resilient polling:** tolerates 5 failed polls before giving up; a 404
  (server restarted) gives a clear message.

---

## Round 4 — blobs, and what the controls even mean

> ok so lets upgrade this, right now ive just been inputing my random stls i
> have and theyve been coming out as just blobs i dont understand the 3 options
> there and i dont know the difference between load direction and fixed face.
> im just uploading stls and expecting a cool 3d spiderweb cool thing but its
> just turning into a blob, what do we need to do to make this an actual working
> cool project, i mostly want it for the looks but obviously it should be
> accurate a little bit.

**Diagnosis:** Blobs are the *expected* output of the old setup: the load was
smeared across an entire bounding-box face, 35% material kept, then aggressive
blur + smoothing. Spiderwebs need a concentrated load, less material, and
distinct force paths. Also the UI expected users to know FEA jargon.

**Fixes:**
- **Concentrated elliptical load patches** (`load_extent`) — force on a small
  central patch instead of a whole face; the single biggest looks win.
- **Multiple anchor faces** (`--fix left,right`) — enables the classic bridge.
- **Scenario preset cards** (🌉 Bridge / 📐 Shelf bracket / 🗼 Tower / 🪝 Hook /
  ⚙️ Custom) with plain-language hints.
- **Live 3D overlay:** blue translucent panels = anchored faces, orange circle +
  arrow = where the force lands and points; updates as controls change.
- **Style chips** (Skeletal / Balanced / Solid) mapping to volume fraction,
  filter radius, and patch size. Human labels + tooltips everywhere.

**Verified:** beam bridge (fix both ends, center patch load) produced a genuine
two-legged branching arch — ASCII cross-sections in the session log.

---

## Round 5 — sticks, splashes, and keeping the shape

> ok so lets upgrade this ... its still like bloby ... i want really like
> internal webbing like those real topology optemized 3d prints, also will this
> work fater on my mac m1 bigg sur? ... we sometimes also want to keep the shape
> of the object we upload thats kinda of an important point, cause everything
> can be just reduced to a stick ... push to another branch

**Diagnosis:** Two fundamental physics gaps. (1) One force direction has one
optimal answer — a single strut ("stick"); real webby prints resist several
force directions at once. (2) Pure compliance optimization owes nothing to the
input silhouette ("splash") — but keeping the *whole* skin would hide all
webbing. Needs a middle ground.

**Fixes** (branch `feature/webbing-and-shape`):
- **Multi-directional load cases** (`load_cases` 1/3/5): tilted companion forces
  solved against the same stiffness matrix — shared assembly and AMG
  preconditioner, so 3 cases cost ~1.8×, not 3×. Webbing chips in the UI
  (▬ none / ╳ crossed / ✳ woven).
- **Shape preservation** (`shape_preserve` 0–1): density floor on the input's
  2-voxel surface shell — silhouette survives where it carries load, erodes
  open where it doesn't.
- **Domain expansion** (`domain_expand` 0–0.3): dilates the design domain so
  struts may grow *outside* the original shape (external buttresses).
- **Mac answer:** M1 ≈ 5–10× faster than the Codespace. On Big Sur, plain pip
  may refuse recent NumPy/SciPy wheels — use Miniforge (conda-forge supports
  macOS ≥ 11); exact steps added to the README. ~500 MB of downloads, no
  compilers, no macOS update needed.

---

## Round 6 — sacred contact surfaces, 1 mm struts, rotation, painting

> ok its still a blob, i think the problem is is that youre removing metirial
> from places where load is supposed to be ... thats where its supposed to be
> flat ... make sure that the webbing and stuff doesnt get thinner than 1 mm
> otherwise it wont print. and the bottom part wasnt flat anymore ... i want you
> to add a feature where we can draw on the original stl for where we want to
> remove metiral, and where the fouce should be ... paint 2 different colors and
> it wont remove metiral from either of them ... also i want the ability to
> rotate in xyz directions the stl on both branches

**Diagnosis:** Correct call by the user — only a 1-voxel skin near the boundary
conditions was protected, and the smoother rounded the contact surfaces into
blobs. Nothing enforced printable strut thickness.

**Fixes** (branch 1, `feature/webbing-and-shape`):
- **Flat solid contact pads:** anchored faces keep their entire footprint as a
  flat solid plate at least `min_feature_mm` deep; the force patch keeps a solid
  boss. The optimizer may only carve *between* pads.
- **`min_feature_mm`** (default 1 mm): floors the density-filter radius so no
  strut prints thinner than the nozzle can handle; also sets pad depth.
- **XYZ rotation:** 90° rotate buttons in the viewer; rotation is baked into the
  preview geometry and shipped to the backend as a 3×3 matrix
  (CLI: `--rotate x,y,z`).

**Fixes** (branch 2, `feature/paint-bc`):
- **Paint-on-model boundary conditions:** 🟦 anchor and 🟧 force brushes painted
  directly on the mesh via raycasting, with erase, clear, and brush-size
  controls; orbiting pauses while painting. Painted points ride along with
  rotations, override the scenario faces, and are never carved (same solid pads).

**Verified:** rotated tower run showed a solid flat full-width base plate and a
solid top boss; painting two bottom corners + one top-center force point
produced an A-frame arch with legs growing exactly from the painted dots.

---

## Round 7 — the cylinder pancake (three stacked engine bugs)

> great i like the settings and the way it works, but its still turning them
> into blobs... imagine just a cylinder, i want the top and bottom to be there
> at the end, but the middle between the whole top and bottom to be
> topologiclaly optemized ... right now its just leaving the bottom and
> squshing the top so it doesnt exsist ...

**Diagnosis:** The user's 65 mm-tall model came out as a 15 mm pancake, and the
job log showed it "converged" after **3 of 60 iterations**. Three compounding
bugs:

1. **Premature convergence:** on a solid shape the first iterations barely move
   (uniform gray everywhere), which the `change < tol` check misread as done —
   the output was the *initial condition*, meshed and smoothed.
2. **Budget starvation:** "keep material 30%" was a *total*; pads + preserved
   skin alone cost more than that, so the free interior collapsed to gray.
3. **No top plate:** the anchored face kept its whole footprint but the loaded
   face only kept a small boss under the force oval — hence "bottom flat, top
   squished out of existence".

**Fixes** (both branches):
- Convergence may not be declared before iteration 12.
- `volfrac` now means fraction of the **carvable** material, on top of
  mandatory pads/floors.
- **`solid_load_face`** (default on, UI checkbox): the entire loaded face stays
  a flat solid plate; the force still acts on the small patch — pad and force
  are separate concepts now.

**Verified:** a 30×65 mm cylinder with UI-default settings kept its full height,
solid top and bottom plates, and a hollow, carved interior — mid-height slice
shows a ring shell with the center removed; ran all iterations.

---

## Round 8 — tooltips, meshiness physics, disk cleanup

> yay!!! much better! by the way the information i/question marks dont work when
> i press them nothing shows, what do we need to do to make it more meshy and
> spiderweby? like those real 3d topological optimizationed stuff? is that a
> resolution setting? ... check the 2 or 3 new models i just made. (also for mac
> make sure that it deletes the old ones on github codespaces i dont care cause
> its gonna get deleted anywhays when i close the codespaces

**Findings on the new models:** the cylinder rerun had **genus 33** — 33 real
through-holes of internal webbing; another model proved rotation worked
end-to-end (optimized in its rotated orientation with proper arches).

**Diagnosis (meshiness):** mostly physics, one real bug. A strut can't be
thinner than ~2 filter radii and the filter can't go below ~1 voxel, so **strut
count scales with resolution** — the showcase look lives at resolution 120–160
(a Mac job, not a Codespace job). The bug: blur + Taubin smoothing ran at full
strength regardless of strut size, erasing exactly the thin struts that make
results look webby.

**Fixes:**
- **Click-to-open tooltips:** the ⓘ icons were native `title=` attributes
  (hover-only, invisible to clicks/taps); now they open styled tooltip bubbles
  on click.
- **Strut-preserving smoothing:** Gaussian sigma scales with the filter radius
  (~rmin/2) and Taubin passes halve for fine lattices.
- **Skeletal style sharpened:** rmin 1.2, 15% material, 12% force area, woven
  webbing, 10% shape-keeping.
- **Disk cleanup:** stale `jobs/` dirs are wiped on every server start (the job
  registry was in-memory anyway).
- Explainer gained the showcase recipe: **Skeletal + ✳ woven + resolution
  120–160, run on a real computer.**

---

## Problems → fixes, at a glance

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | "This page isn't working" | Server never started | Start uvicorn; docs |
| 2 | `Unexpected token '<'` on upload | Codespaces proxy rejects >16 MiB bodies with HTML 413 | 8 MB chunked uploads + readable errors |
| 3 | "It froze" | ~1 min/iteration + watching a duplicate queued job after refresh | Cancel, reattach-on-reload, ETA, resilient polling |
| 4 | Everything's a blob | Whole-face loads, high volfrac, FEA-jargon UI | Load patches, multi-anchors, presets, BC overlay, styles |
| 5 | Stick / splash, no webbing | Single load case; nothing preserves the silhouette | Multi-directional load cases, shape_preserve, domain_expand |
| 6 | Contact faces rounded away; unprintable struts | Only 1-voxel skin protected; no min feature size | Flat solid pads, min_feature_mm, rotation, paint-on-model BCs |
| 7 | Tall model → squashed pancake | Convergence at iter 3; pads+floors ate the budget; no top plate | Min-12-iteration guard, carvable-budget volfrac, solid_load_face |
| 8 | ⓘ dead; webs too chunky | Hover-only tooltips; smoothing erased thin struts | Click tooltips, adaptive smoothing, sharper Skeletal, resolution guidance |

## Branch map

- `main` — original app as first committed.
- `feature/webbing-and-shape` — everything except painting.
- `feature/paint-bc` — everything, including paint-on-model boundary conditions
  (superset; the branch this file lives on).

## The recipes that came out of all this

```bash
# webby bridge that keeps its outline (CLI)
python optimize.py test_shapes/beam.stl bridge.stl \
    --fix left,right --load top --dir=-z --load-extent 0.15 \
    --load-cases 3 --shape-preserve 0.35 --volfrac 0.22 --rmin 1.5
```

Web UI showcase recipe: **Skeletal + ✳ woven + resolution 120–160**, on a real
computer (M1 ≈ 5–10× the Codespace; use Miniforge on Big Sur — see README).
Draft on the Codespace at resolution 48–64 first to dial in anchors and forces.
