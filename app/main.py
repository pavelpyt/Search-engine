"""FastAPI app: serves the UI, runs pipelines as jobs and streams their events over SSE.

Security for a public demo:
- With LOGIN_USER_SHA256 / LOGIN_PASS_SHA256 set, only /login and /static answer
  without a session. The server keeps nothing but the SHA-256 hashes.
- POST /api/session exchanges the credentials (in the JSON body, never in a URL)
  for an HttpOnly session cookie. Repeated failures from one IP are blocked.
- POST /api/jobs takes the question in the body; the SSE URL carries only the job id.
  A follow-up names its parent job; the context stays on the server, the browser never supplies it.
- A job's events and trace are readable only by the session that started it.
- API responses are `Cache-Control: no-store`; logs never contain the code or cookie.
- Jobs survive a dropped connection: the stream replays from Last-Event-ID.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .agents import AGENTS, MODEL_PLAN, MODELS
from .llm import is_mock, make_client
from .orchestrator import Pipeline
from .skills import list_skills

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.getenv("LOG_DIR", ROOT / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))

LOGIN_USER_SHA256 = os.getenv("LOGIN_USER_SHA256", "").strip().lower()
LOGIN_PASS_SHA256 = os.getenv("LOGIN_PASS_SHA256", "").strip().lower()
LOGIN_REQUIRED = bool(LOGIN_USER_SHA256 or LOGIN_PASS_SHA256)  # one hash missing locks everyone out, never opens up
MAX_FAILED_LOGINS, FAILED_LOGIN_WINDOW = 10, 900
RUNS_PER_HOUR = int(os.getenv("RUNS_PER_HOUR", "20"))
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"
SESSION_TTL = 12 * 3600
MAX_QUESTION = 500
MAX_JOBS_IN_MEMORY = 50
COOKIE = "ra_session"

app = FastAPI(title="Research Agents", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

SESSIONS: dict[str, float] = {}                 # session id -> expiry
_runs_by_ip: dict[str, deque] = defaultdict(deque)
_failed_logins: dict[str, deque] = defaultdict(deque)
CONCURRENCY = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_RUNS", "3")))


class Job:
    def __init__(self, owner: str, question: str):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        self.owner = owner
        self.question = question
        self.memory: dict = {}  # what a follow-up to this job inherits
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


def _has_session(request: Request) -> bool:
    exp = SESSIONS.get(request.cookies.get(COOKIE, ""))
    return bool(exp and exp >= time.time())


def _session(request: Request) -> str:
    if not _has_session(request):
        raise HTTPException(401, "No valid session")
    return request.cookies[COOKIE]


def _require_login(request: Request):
    if LOGIN_REQUIRED:
        _session(request)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _credentials_ok(username: str, password: str) -> bool:
    user_ok = secrets.compare_digest(_sha256(username), LOGIN_USER_SHA256)
    pass_ok = secrets.compare_digest(_sha256(password), LOGIN_PASS_SHA256)  # always both: no timing hint
    return user_ok and pass_ok


def _login_blocked(ip: str) -> bool:
    q, now = _failed_logins[ip], time.time()
    while q and now - q[0] > FAILED_LOGIN_WINDOW:
        q.popleft()
    return len(q) >= MAX_FAILED_LOGINS


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
async def index(request: Request):
    if LOGIN_REQUIRED and not _has_session(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/login")
async def login_page(request: Request):
    if not LOGIN_REQUIRED or _has_session(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(ROOT / "static" / "login.html")


@app.get("/api/config")
async def config(request: Request):
    _require_login(request)
    return {
        "mock": is_mock(),
        "login_required": LOGIN_REQUIRED,
        "agents": [{"key": a.key, "label": a.label, "emoji": a.emoji, "role": a.role,
                    "skills": a.skill_names} for a in AGENTS.values()],
        "models": MODELS, "model_plan": MODEL_PLAN, "skills": list_skills(),
    }


class SessionIn(BaseModel):
    username: str = Field("", max_length=256)
    password: str = Field("", max_length=256)


@app.post("/api/session")
async def create_session(body: SessionIn, request: Request, response: Response):
    if LOGIN_REQUIRED:
        ip = _client_ip(request)
        if _login_blocked(ip):
            raise HTTPException(429, "Příliš mnoho neúspěšných pokusů, zkus to později.")
        if not _credentials_ok(body.username, body.password):
            _failed_logins[ip].append(time.time())
            raise HTTPException(401, "Neplatné přihlašovací údaje.")
    now = time.time()
    for sid in [s for s, exp in SESSIONS.items() if exp < now]:
        SESSIONS.pop(sid, None)
    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = now + SESSION_TTL
    secure = COOKIE_SECURE and (request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(COOKIE, sid, max_age=SESSION_TTL, httponly=True, secure=secure, samesite="lax")
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    SESSIONS.pop(request.cookies.get(COOKIE, ""), None)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/skills/{name}")
async def skill_file(name: str, request: Request):
    _require_login(request)
    if name not in {s["name"] for s in list_skills()}:
        raise HTTPException(404)
    return FileResponse(ROOT / "skills" / name / "SKILL.md", media_type="text/markdown; charset=utf-8")


class JobIn(BaseModel):
    question: str
    parent_id: str | None = None  # the finished job this question follows up on


@app.post("/api/jobs")
async def create_job(body: JobIn, request: Request):
    sid = _session(request)
    question = body.question.strip()[:MAX_QUESTION]
    if len(question) < 3:
        raise HTTPException(422, "Question too short")
    parent = _own_job(body.parent_id, request) if body.parent_id else None
    if parent and not parent.done:
        raise HTTPException(409, "Předchozí běh ještě neskončil.")
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
            await emit({"type": "run_started", "question": question, "parent_id": body.parent_id})
            if CONCURRENCY.locked():
                await emit({"type": "log", "level": "info", "msg": "Čekám ve frontě na volný běh…"})
            async with CONCURRENCY:
                pipeline = Pipeline(make_client(), emit, memory=parent.memory if parent else None)
                job.memory = pipeline.next_memory(await pipeline.run(question))
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
