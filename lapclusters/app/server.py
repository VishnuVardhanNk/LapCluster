"""The local web server behind the dashboard.

It listens on this laptop only. Other laptops never talk to it; they share
state through Redis, and each runs its own copy of this server.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from lapclusters import config, discovery
from lapclusters.app import views
from lapclusters.app.node import HOST, ConnectError, Node
from lapclusters.orchestrator import (
    DEFAULT_QUESTION,
    KINDS,
    NEEDS_QUESTION,
    NEEDS_SOURCE,
    build_answer_report,
    build_report,
    fill_job,
    job_answer,
    prepare_job,
    retry_job,
)
from lapclusters.repo import RepoError
from lapclusters.taskqueue import CONNECTION_ERRORS, connection_problem

STATIC = Path(__file__).parent / "static"
LOCAL_NAMES = {"127.0.0.1", "localhost", "testserver"}


class ConnectBody(BaseModel):
    role: str
    password: str = ""
    name: str = ""
    address: str = ""
    port: int = 6379
    follow: bool = True


class WorkerBody(BaseModel):
    action: str


class ModelBody(BaseModel):
    worker: str
    model: str


class ReviewBody(BaseModel):
    source: str = ""
    kind: str = "review"
    question: str = ""


def _problem(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(node: Node | None = None) -> FastAPI:
    node = node or Node()
    app = FastAPI(title="LapClusters", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.node = node
    # Know whether Ollama is up before the first page load, so the Connect
    # screen never briefly claims it is not running.
    node.runtime.refresh_models()
    last_model_check = {"at": time.monotonic()}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # The server binds to this laptop only, but a web page open in the
        # browser could still try to call it. Requiring a local Host header and
        # a custom header on anything that changes state shuts that out.
        host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
        if host not in LOCAL_NAMES:
            return _problem("This dashboard only answers on this laptop.", 403)
        if request.method != "GET" and request.headers.get("x-lapclusters") != "1":
            return _problem("Missing request header.", 403)
        return await call_next(request)

    @app.exception_handler(Exception)
    async def unreachable(request: Request, exc: Exception):
        if isinstance(exc, CONNECTION_ERRORS):
            node.lost_connection()
            return _problem(connection_problem(exc), 503)
        if isinstance(exc, discovery.DiscoveryError):
            return _problem(str(exc), 503)
        return _problem(f"{type(exc).__name__}: {exc}", 500)

    # --- pages -----------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # --- connecting ------------------------------------------------------

    @app.get("/api/discover")
    def discover():
        hosts = discovery.find_hosts(timeout_s=1.5)
        return {
            "hosts": [
                {"name": h.name, "address": h.address, "port": h.redis_port, "is_me": h.name == node.name}
                for h in hosts
            ]
        }

    @app.post("/api/connect")
    def connect_(body: ConnectBody):
        try:
            if body.role == HOST:
                node.connect_host(body.password, body.port)
            elif body.address:
                node.connect_member(
                    body.password, body.name, body.address, body.port, follow=body.follow
                )
            else:
                return _problem("Choose a host to join.")
        except ConnectError as exc:
            return _problem(str(exc))
        return {"ok": True}

    @app.post("/api/disconnect")
    def disconnect():
        node.leave()
        return {"ok": True}

    # --- state -----------------------------------------------------------

    @app.get("/api/state")
    def state(job: str = ""):
        if time.monotonic() - last_model_check["at"] > 10 and node.worker_state == "stopped":
            # With no worker running nothing else keeps the model list fresh.
            last_model_check["at"] = time.monotonic()
            node.runtime.refresh_models()
        saved = urlsplit(config.REDIS_URL)
        view = {
            "me": node.name,
            "role": node.role,
            "host": node.host,
            "announcing": node.announcing,
            "connected": False,
            "error": "",
            "worker": {
                "state": node.worker_state,
                "note": node.worker_note or node.runtime.problem(),
            },
            "model": config.MODEL,
            "models": node.runtime.models,
            "ollama": node.runtime.ollama_ok,
            "vision": node.runtime.vision,
            "restore_problem": node.restore_problem,
            "saved_password": unquote(saved.password or "") if node.role is None else "",
            "saved_port": saved.port or 6379,
        }
        if node.role is None:
            return view
        try:
            queue = node.queue()
            view["now"] = queue.now()
            view["workers"] = queue.worker_details()
            jobs = queue.recent_jobs(20)
            chosen = job if job in jobs else (jobs[0] if jobs else "")
            view["job"] = (
                views.job_view(queue, chosen, close=node.role == HOST) if chosen else None
            )
            view["jobs"] = [s for s in (views.job_summary(queue, j) for j in jobs) if s]
            view["events"] = queue.events(60)
            view["connected"] = True
        except CONNECTION_ERRORS as exc:
            node.lost_connection()
            view["error"] = connection_problem(exc)
        except discovery.DiscoveryError as exc:
            node.lost_connection()
            view["error"] = str(exc)
        return view

    # --- this laptop's worker and the models -----------------------------

    @app.post("/api/worker")
    def worker(body: WorkerBody):
        if body.action == "start":
            problem = node.start_worker()
            if problem:
                return _problem(problem)
        elif body.action == "stop":
            node.stop_worker()
        else:
            return _problem("Unknown action.")
        return {"ok": True, "state": node.worker_state}

    @app.post("/api/model")
    def model(body: ModelBody):
        if body.worker == node.name:
            problem = node.set_own_model(body.model)
            return _problem(problem) if problem else {"ok": True, "applied": True}
        if node.role != HOST:
            return _problem("Only the host can change another laptop's model.", 403)
        queue = node.queue()
        target = queue.worker_details().get(body.worker)
        if target is None:
            return _problem(f"{body.worker} is not connected.")
        if "models" not in target:
            return _problem(
                f"{body.worker} is running an older worker that cannot switch models remotely."
            )
        if body.model not in target["models"]:
            return _problem(f"{body.model} is not installed on {body.worker}.")
        queue.send_command(
            body.worker, {"cmd": "set_model", "model": body.model, "by": node.name}
        )
        # The other laptop applies it at its next heartbeat, a few seconds away.
        return {"ok": True, "applied": False}

    # --- reviews ---------------------------------------------------------

    @app.post("/api/reviews")
    def start_review(body: ReviewBody):
        if node.role != HOST:
            return _problem("Only the host can start a job.", 403)
        if body.kind not in KINDS:
            return _problem("Unknown kind of job.")
        source = body.source.strip().strip('"') if body.kind in NEEDS_SOURCE else ""
        question = body.question.strip() if body.kind in NEEDS_QUESTION else ""
        if body.kind in NEEDS_SOURCE and not source:
            return _problem("Enter a folder or a git URL.")
        if body.kind in ("prompt", "code") and not question:
            return _problem("Type what you want to ask.")
        if body.kind == "ask" and not question:
            question = DEFAULT_QUESTION
        queue = node.queue()
        job_id = prepare_job(queue, source, started_by=node.name, kind=body.kind, question=question)

        def fill() -> None:
            try:
                fill_job(queue, job_id, source)
            except RepoError:
                pass  # fill_job has recorded the reason on the job
            except CONNECTION_ERRORS:
                node.lost_connection()
            except Exception as exc:
                queue.close_job(
                    job_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=f"{time.time():.3f}",
                )

        # Cloning can take a while, so it must not hold up the reply.
        threading.Thread(target=fill, daemon=True).start()
        return {"ok": True, "job": job_id}

    @app.post("/api/reviews/{job_id}/cancel")
    def cancel_review(job_id: str):
        if node.role != HOST:
            return _problem("Only the host can cancel a review.", 403)
        queue = node.queue()
        if not queue.job_meta(job_id):
            return _problem("No such review.", 404)
        return {"ok": True, "cancelled": views.cancel_job(queue, job_id)}

    @app.post("/api/reviews/{job_id}/retry")
    def retry_review(job_id: str):
        if node.role != HOST:
            return _problem("Only the host can run failed tasks again.", 403)
        queue = node.queue()
        if not queue.job_meta(job_id):
            return _problem("No such job.", 404)
        return {"ok": True, "retried": retry_job(queue, job_id)}

    @app.post("/api/history/clear")
    def clear_history():
        if node.role != HOST:
            return _problem("Only the host can clear the history.", 403)
        return {"ok": True, "deleted": views.clear_history(node.queue())}

    @app.get("/api/reviews/{job_id}/findings")
    def findings(job_id: str):
        queue = node.queue()
        if not queue.job_meta(job_id):
            return _problem("No such review.", 404)
        return views.findings_view(queue, job_id)

    @app.get("/api/reviews/{job_id}/report.md")
    def report(job_id: str):
        queue = node.queue()
        meta = queue.job_meta(job_id)
        if not meta:
            return _problem("No such review.", 404)
        skipped = [tuple(item) for item in views._loads(meta.get("skipped"), [])]
        rows = views.report_rows(queue, job_id)
        if meta.get("kind", "review") == "review":
            text = build_report(meta.get("source", ""), rows, skipped)
        else:
            text = build_answer_report(
                meta.get("source", ""), meta.get("question", ""), job_answer(queue, job_id),
                rows, skipped,
            )
        return PlainTextResponse(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="review-report.md"'},
        )

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        view = views.task_view(node.queue(), task_id)
        return view if view is not None else _problem("No such file.", 404)

    @app.get("/api/tasks/{task_id}/output")
    def output(task_id: str, offset: int = 0):
        text, next_offset = node.queue().read_output(task_id, max(offset, 0))
        return {"text": text, "next": next_offset, "restarted": next_offset < offset}

    return app
