"""FastAPI backend for the topology optimizer web app.

Run with:  uvicorn main:app --host 0.0.0.0 --port 8000

Jobs run on a single background worker thread (optimizations are CPU-bound
and take minutes); additional submissions queue up. The job store is
in-memory — restarting the server clears history (single-user tool).
"""

from __future__ import annotations

import queue
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from topopt.pipeline import DIRECTIONS, FACES, Params, run_pipeline

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # generous cap for 1-2M triangle STLs
JOBS_DIR = Path(__file__).parent / "jobs"


@dataclass
class Job:
    id: str
    status: str = "queued"  # queued | running | done | error
    phase: str = "queued"
    iteration: int = 0
    total_iterations: int = 0
    compliance: float | None = None
    message: str = ""
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    input_path: str = ""
    output_path: str = ""
    params: Params = field(default_factory=Params)
    result: dict | None = None

    def to_public(self, position: int | None = None) -> dict:
        elapsed = None
        if self.started is not None:
            elapsed = round((self.finished or time.time()) - self.started, 1)
        return {
            "id": self.id,
            "status": self.status,
            "phase": self.phase,
            "iteration": self.iteration,
            "total_iterations": self.total_iterations,
            "compliance": self.compliance,
            "message": self.message,
            "error": self.error,
            "queue_position": position,
            "elapsed_seconds": elapsed,
            "result": self.result,
        }


JOBS: dict[str, Job] = {}
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()
LOCK = threading.Lock()
_STOP = threading.Event()


def _worker() -> None:
    """Single worker: one optimization at a time, others wait in the queue."""
    while not _STOP.is_set():
        try:
            job_id = JOB_QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue
        with LOCK:
            job = JOBS.get(job_id)
        if job is None:
            continue
        job.status = "running"
        job.started = time.time()

        def progress(phase: str, detail: dict) -> None:
            with LOCK:
                job.phase = phase
                if "iteration" in detail:
                    job.iteration = detail["iteration"]
                    job.total_iterations = detail.get("total", job.total_iterations)
                if "compliance" in detail:
                    job.compliance = detail["compliance"]
                if "message" in detail:
                    job.message = detail["message"]

        try:
            result = run_pipeline(job.input_path, job.output_path, job.params, progress)
            with LOCK:
                job.result = result.to_dict()
                job.status = "done"
                job.phase = "done"
        except Exception as exc:  # report, never crash the worker
            with LOCK:
                job.status = "error"
                job.phase = "error"
                job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished = time.time()
            JOB_QUEUE.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS_DIR.mkdir(exist_ok=True)
    thread = threading.Thread(target=_worker, name="topopt-worker", daemon=True)
    thread.start()
    yield
    _STOP.set()


app = FastAPI(title="Topology Optimizer", lifespan=lifespan)


def _queue_position(job_id: str) -> int | None:
    with LOCK:
        queued = [j.id for j in sorted(JOBS.values(), key=lambda j: j.created) if j.status == "queued"]
    return queued.index(job_id) + 1 if job_id in queued else None


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    resolution: int = Form(96),
    volfrac: float = Form(0.35),
    fix_face: str = Form("bottom"),
    load_face: str = Form("top"),
    load_dir: str = Form("-z"),
    rmin: float = Form(2.0),
    max_iter: int = Form(60),
) -> JSONResponse:
    params = Params(
        resolution=resolution,
        volfrac=volfrac,
        fix_face=fix_face,
        load_face=load_face,
        load_dir=load_dir,
        rmin=rmin,
        max_iter=max_iter,
    )
    try:
        params.validate()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True)
    input_path = job_dir / "input.stl"

    size = 0
    with tempfile.NamedTemporaryFile(dir=job_dir, delete=False) as tmp:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                tmp.close()
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(status_code=413, detail="file too large (max 200 MB)")
            tmp.write(chunk)
        tmp_path = tmp.name
    if size == 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail="empty upload")
    Path(tmp_path).rename(input_path)

    job = Job(
        id=job_id,
        input_path=str(input_path),
        output_path=str(job_dir / "output.stl"),
        params=params,
        total_iterations=max_iter,
    )
    with LOCK:
        JOBS[job_id] = job
    JOB_QUEUE.put(job_id)
    return JSONResponse(job.to_public(_queue_position(job_id)), status_code=201)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_public(_queue_position(job_id))


def _result_file(job_id: str) -> Path:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "done":
        raise HTTPException(status_code=409, detail=f"job is {job.status}, not done")
    path = Path(job.output_path)
    if not path.exists():
        raise HTTPException(status_code=500, detail="result file missing")
    return path


@app.get("/api/jobs/{job_id}/result")
def download_result(job_id: str) -> FileResponse:
    return FileResponse(
        _result_file(job_id),
        media_type="model/stl",
        filename="optimized.stl",
    )


@app.get("/api/jobs/{job_id}/preview")
def preview_result(job_id: str) -> FileResponse:
    """The result as binary STL for the in-browser Three.js viewer."""
    return FileResponse(_result_file(job_id), media_type="model/stl")


@app.get("/api/meta")
def meta() -> dict:
    """Parameter choices for the frontend dropdowns."""
    return {"faces": list(FACES), "directions": sorted(DIRECTIONS)}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
