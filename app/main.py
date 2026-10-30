"""FastAPI app: serves the UI and streams pipeline events over SSE."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .agents import AGENTS, MODEL_PLAN, MODELS
from .llm import is_mock, make_client
from .orchestrator import Pipeline
from .skills import list_skills

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.getenv("LOG_DIR", ROOT / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

ACCESS_CODE = os.getenv("ACCESS_CODE", "")
RUNS_PER_HOUR = int(os.getenv("RUNS_PER_HOUR", "20"))
MAX_QUESTION = 500

app = FastAPI(title="Research Agents")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

_runs_by_ip: dict[str, deque] = defaultdict(deque)
RUNS: "OrderedDict[str, list[dict]]" = OrderedDict()   # last MAX_RUNS_IN_MEMORY runs, older ones live in logs/
MAX_RUNS_IN_MEMORY = 50
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"      # set to 1 behind Caddy/nginx
CONCURRENCY = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_RUNS", "3")))


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


@app.get("/api/skills/{name}")
async def skill_file(name: str):
    names = {s["name"] for s in list_skills()}
    if name not in names:
        raise HTTPException(404)
    return FileResponse(ROOT / "skills" / name / "SKILL.md", media_type="text/markdown; charset=utf-8")


@app.get("/api/runs/{run_id}")
async def run_log(run_id: str):
    if run_id in RUNS:
        return JSONResponse(RUNS[run_id])
    path = LOG_DIR / f"{run_id}.jsonl"
    if not path.exists():
        raise HTTPException(404)
    return JSONResponse([json.loads(line) for line in path.read_text().splitlines()])


def _rate_limited(ip: str) -> bool:
    q, now = _runs_by_ip[ip], time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RUNS_PER_HOUR:
        return True
    q.append(now)
    return False


@app.get("/api/run")
async def run(request: Request, q: str = Query(..., min_length=3), code: str = ""):
    question = q.strip()[:MAX_QUESTION]
    # Refusals go out as an SSE event: EventSource cannot read an HTTP 401/429 body.
    refusal = None
    if ACCESS_CODE and code != ACCESS_CODE:
        refusal = "Špatný přístupový kód."
    else:
        ip = request.client.host if request.client else "?"
        if TRUST_PROXY and request.headers.get("x-forwarded-for"):
            ip = request.headers["x-forwarded-for"].split(",")[-1].strip()  # the hop our proxy added
        if _rate_limited(ip):
            refusal = "Limit dotazů na hodinu vyčerpán, zkus to později."
    if refusal:
        async def refuse():
            yield {"data": json.dumps({"type": "error", "msg": refusal}, ensure_ascii=False)}
            yield {"data": json.dumps({"type": "run_finished"})}
        return EventSourceResponse(refuse())

    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    queue: asyncio.Queue = asyncio.Queue()
    events: list[dict] = []
    RUNS[run_id] = events
    while len(RUNS) > MAX_RUNS_IN_MEMORY:
        RUNS.popitem(last=False)
    log_file = (LOG_DIR / f"{run_id}.jsonl").open("a", encoding="utf-8")

    async def emit(event: dict):
        event = {"ts": round(time.time(), 3), "run_id": run_id, **event}
        events.append(event)
        log_file.write(json.dumps(event, ensure_ascii=False) + "\n")
        log_file.flush()
        await queue.put(event)

    async def worker():
        try:
            await emit({"type": "run_started", "question": question})
            if CONCURRENCY.locked():
                await emit({"type": "log", "level": "info", "msg": "Čekám ve frontě na volný běh…"})
            async with CONCURRENCY:
                await Pipeline(make_client(), emit).run(question)
        except asyncio.CancelledError:
            await emit({"type": "error", "msg": "Run cancelled (client disconnected)"})
            raise
        except Exception as exc:
            await emit({"type": "error", "msg": str(exc)})
        finally:
            await emit({"type": "run_finished"})
            log_file.close()

    task = asyncio.create_task(worker())

    async def stream():
        try:
            while True:
                event = await queue.get()
                yield {"data": json.dumps(event, ensure_ascii=False)}
                if event["type"] == "run_finished":
                    break
        finally:
            if not task.done():
                task.cancel()

    return EventSourceResponse(stream(), ping=15)
