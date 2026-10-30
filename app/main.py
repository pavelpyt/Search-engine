"""FastAPI app: serves the UI, runs pipelines as jobs and streams their events over SSE.

Security for a public demo:
- POST /api/session exchanges the access code (in the JSON body, never in a URL)
  for an HttpOnly session cookie.
- POST /api/jobs takes the question in the body; the SSE URL carries only the job id.
- A job's events and trace are readable only by the session that started it.
- API responses are `Cache-Control: no-store`; logs never contain the code or cookie.
- Jobs survive a dropped connection: the stream replays from Last-Event-ID.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .agents import AGENTS, MODEL_PLAN, MODELS
from .llm import is_mock, make_client
from .orchestrator import Pipeline
from .skills import list_skills

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.getenv("LOG_DIR", ROOT / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))

ACCESS_CODE = os.getenv("ACCESS_CODE", "")
RUNS_PER_HOUR = int(os.getenv("RUNS_PER_HOUR", "20"))
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"
SESSION_TTL = 12 * 3600
MAX_QUESTION = 500
MAX_JOBS_IN_MEMORY = 50
COOKIE = "ra_session"

app = FastAPI(title="Research Agents")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

SESSIONS: dict[str, float] = {}                 # session id -> expiry
_runs_by_ip: dict[str, deque] = defaultdict(deque)
CONCURRENCY = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_RUNS", "3")))


class Job:
    def __init__(self, owner: str, question: str):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        self.owner = owner
        self.question = question
        self.events: list[dict] = []
        self.done = False
        self.cond = asyncio.Condition()

    async def add(self, event: dict):
        async with self.cond:
            self.events.append(event)
            self.cond.notify_all()


JOBS: "OrderedDict[str, Job]" = OrderedDict()


def _cleanup_logs():
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for f in LOG_DIR.glob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


_cleanup_logs()


@app.middleware("http")
async def no_store(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _session(request: Request) -> str:
    sid = request.cookies.get(COOKIE, "")
    exp = SESSIONS.get(sid)
    if not exp or exp < time.time():
        raise HTTPException(401, "No valid session")
    return sid


def _client_ip(request: Request) -> str:
    ip = request.client.host if request.client else "?"
    if TRUST_PROXY and request.headers.get("x-forwarded-for"):
        ip = request.headers["x-forwarded-for"].split(",")[-1].strip()  # the hop our proxy added
    return ip


def _rate_limited(ip: str) -> bool:
    q, now = _runs_by_ip[ip], time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RUNS_PER_HOUR:
        return True
    q.append(now)
    return False


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/config")
async def config():
    return {
        "mock": is_mock(),
        "access_code_required": bool(ACCESS_CODE),
        "agents": [{"key": a.key, "label": a.label, "emoji": a.emoji, "role": a.role,
                    "skills": a.skill_names} for a in AGENTS.values()],
        "models": MODELS, "model_plan": MODEL_PLAN, "skills": list_skills(),
    }


class SessionIn(BaseModel):
    code: str = ""


@app.post("/api/session")
async def create_session(body: SessionIn, request: Request, response: Response):
    if ACCESS_CODE and not secrets.compare_digest(body.code, ACCESS_CODE):
        raise HTTPException(401, "Wrong access code")
    now = time.time()
    for sid in [s for s, exp in SESSIONS.items() if exp < now]:
        SESSIONS.pop(sid, None)
    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = now + SESSION_TTL
    secure = COOKIE_SECURE and (request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(COOKIE, sid, max_age=SESSION_TTL, httponly=True, secure=secure, samesite="lax")
    return {"ok": True}


@app.get("/api/skills/{name}")
async def skill_file(name: str):
    if name not in {s["name"] for s in list_skills()}:
        raise HTTPException(404)
    return FileResponse(ROOT / "skills" / name / "SKILL.md", media_type="text/markdown; charset=utf-8")


class JobIn(BaseModel):
    question: str


@app.post("/api/jobs")
async def create_job(body: JobIn, request: Request):
    sid = _session(request)
    question = body.question.strip()[:MAX_QUESTION]
    if len(question) < 3:
        raise HTTPException(422, "Question too short")
    if _rate_limited(_client_ip(request)):
        raise HTTPException(429, "Limit dotazů na hodinu vyčerpán, zkus to později.")
    job = Job(sid, question)
    JOBS[job.id] = job
    while len(JOBS) > MAX_JOBS_IN_MEMORY:
        JOBS.popitem(last=False)
    log_file = (LOG_DIR / f"{job.id}.jsonl").open("a", encoding="utf-8")

    async def emit(event: dict):
        event = {"ts": round(time.time(), 3), "run_id": job.id, **event}
        log_file.write(json.dumps(event, ensure_ascii=False) + "\n")
        log_file.flush()
        await job.add(event)

    async def worker():
        try:
            await emit({"type": "run_started", "question": question})
            if CONCURRENCY.locked():
                await emit({"type": "log", "level": "info", "msg": "Čekám ve frontě na volný běh…"})
            async with CONCURRENCY:
                await Pipeline(make_client(), emit).run(question)
        except Exception as exc:  # the pipeline reports its own failures; this is a last resort
            await emit({"type": "error", "msg": str(exc)})
        finally:
            await emit({"type": "run_finished"})
            job.done = True
            log_file.close()

    asyncio.create_task(worker())  # runs to completion even if the browser disconnects
    return {"job_id": job.id}


def _own_job(job_id: str, request: Request) -> Job:
    sid = _session(request)
    job = JOBS.get(job_id)
    if not job or not secrets.compare_digest(job.owner, sid):
        raise HTTPException(404, "Unknown job")
    return job


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    job = _own_job(job_id, request)
    try:
        start = int(request.headers.get("last-event-id", "-1")) + 1
    except ValueError:
        start = 0

    async def stream():
        i = start
        while True:
            async with job.cond:
                while i >= len(job.events) and not job.done:
                    await job.cond.wait()
                batch = job.events[i:]
            for event in batch:
                yield {"id": str(i), "data": json.dumps(event, ensure_ascii=False)}
                i += 1
            if job.done and i >= len(job.events):
                break

    return EventSourceResponse(stream(), ping=15)


@app.get("/api/runs/{job_id}")
async def run_log(job_id: str, request: Request):
    return JSONResponse(_own_job(job_id, request).events)
