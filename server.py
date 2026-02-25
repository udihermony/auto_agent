#!/usr/bin/env python3
"""
Local web server for observing and steering the agent.

Usage:
    python server.py

Then open http://localhost:8000
"""

import json, asyncio
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

ROOT       = Path(__file__).parent.resolve()
STATE_DIR  = ROOT / "state"
TOOLS_DIR  = ROOT / "tools"
MEMORY_DIR = ROOT / "memory"
AGENTS_DIR = ROOT / "agents"

app = FastAPI(title="Agent UI")


# ─── UI ────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return (ROOT / "web" / "index.html").read_text(encoding="utf-8")


# ─── Live stream (SSE) ─────────────────────────────────────────────────────────
@app.get("/stream")
async def stream_events(request: Request):
    """Server-Sent Events: tails state/stream.jsonl and pushes new lines."""
    stream_file = STATE_DIR / "stream.jsonl"
    stream_file.touch()

    async def generator():
        # Send last 300 lines of existing content so the UI has context on connect
        try:
            content = stream_file.read_bytes()
            lines = content.decode("utf-8", errors="replace").splitlines()
            for line in lines[-300:]:
                if line.strip():
                    yield f"data: {line.strip()}\n\n"
            pos = len(content)
        except Exception:
            pos = 0

        # Tail for new lines
        while True:
            if await request.is_disconnected():
                break
            try:
                with open(stream_file, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos += len(chunk)
                if chunk:
                    for line in chunk.decode("utf-8", errors="replace").splitlines():
                        if line.strip():
                            yield f"data: {line.strip()}\n\n"
            except Exception:
                pass
            await asyncio.sleep(0.2)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── State snapshot ────────────────────────────────────────────────────────────
@app.get("/state")
async def get_state():
    def rj(path, default):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return default

    return {
        "status":  rj(STATE_DIR / "status.json", {}),
        "tools":   rj(TOOLS_DIR / "__registry__.json", {}),
        "memory":  (MEMORY_DIR / "semantic.md").read_text(encoding="utf-8")
                   if (MEMORY_DIR / "semantic.md").exists() else "",
        "paused":  (STATE_DIR / "pause").exists(),
    }


# ─── Session history ───────────────────────────────────────────────────────────
@app.get("/sessions")
async def get_sessions():
    sessions_dir = MEMORY_DIR / "sessions"
    if not sessions_dir.exists():
        return []
    result = []
    for d in sorted(sessions_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        task_file   = d / "task.txt"
        result_file = d / "result.json"
        result.append({
            "id":     d.name,
            "task":   task_file.read_text(encoding="utf-8").strip() if task_file.exists() else "",
            "done":   result_file.exists(),
            "result": json.loads(result_file.read_text(encoding="utf-8"))
                      if result_file.exists() else None,
        })
    return result


@app.get("/sessions/{session_id}/stream")
async def session_stream(session_id: str):
    path = MEMORY_DIR / "sessions" / session_id / "stream.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ─── Sub-agents ────────────────────────────────────────────────────────────────
@app.get("/agents")
async def get_agents():
    if not AGENTS_DIR.exists():
        return []
    result = []
    for d in sorted(AGENTS_DIR.iterdir()):
        if not d.is_dir():
            continue

        def rj(name, default=None):
            p = d / name
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return default

        result.append({
            "id":     d.name,
            "task":   (rj("task.json") or {}).get("task", ""),
            "status": rj("status.json") or {},
            "done":   (d / "result.json").exists(),
            "result": rj("result.json"),
        })
    return result


@app.get("/agents/{agent_id}/stream")
async def agent_stream(agent_id: str):
    path = AGENTS_DIR / agent_id / "stream.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ─── Agent prompt ──────────────────────────────────────────────────────────────
@app.get("/prompt")
async def get_prompt():
    p = STATE_DIR / "agent_prompt.txt"
    if p.exists():
        return {"content": p.read_text(encoding="utf-8"), "saved": True}
    # File not yet created — extract AGENT_SYSTEM from seed.py as text
    try:
        import re
        seed_text = (ROOT / "seed.py").read_text(encoding="utf-8")
        m = re.search(r'AGENT_SYSTEM\s*=\s*"""([\s\S]*?)"""', seed_text)
        if m:
            return {"content": m.group(1), "saved": False}
    except Exception:
        pass
    return {"content": "", "saved": False}


@app.post("/prompt")
async def save_prompt(body: dict):
    content = body.get("content", "")
    if not content.strip():
        return {"ok": False, "error": "Prompt cannot be empty"}
    (STATE_DIR / "agent_prompt.txt").write_text(content, encoding="utf-8")
    return {"ok": True}


@app.post("/prompt/reset")
async def reset_prompt():
    p = STATE_DIR / "agent_prompt.txt"
    if p.exists():
        p.unlink()
    return {"ok": True}


# ─── Control endpoints ─────────────────────────────────────────────────────────
@app.post("/task")
async def post_task(body: dict):
    """Send a new task (when agent is idle)."""
    (STATE_DIR / "inbox.json").write_text(
        json.dumps({"type": "task", "content": body.get("task", "")}),
        encoding="utf-8",
    )
    return {"ok": True}


@app.post("/steer")
async def steer(body: dict):
    """Inject a steering message (when agent is working)."""
    (STATE_DIR / "inbox.json").write_text(
        json.dumps({"type": "steer", "content": body.get("message", "")}),
        encoding="utf-8",
    )
    return {"ok": True}


@app.post("/abort")
async def abort(body: dict):
    """Abort current task with an optional new goal."""
    (STATE_DIR / "inbox.json").write_text(
        json.dumps({"type": "abort", "content": body.get("message", "Aborted")}),
        encoding="utf-8",
    )
    return {"ok": True}


@app.post("/pause")
async def pause():
    (STATE_DIR / "pause").touch()
    return {"ok": True}


@app.post("/resume")
async def resume():
    p = STATE_DIR / "pause"
    if p.exists():
        p.unlink()
    return {"ok": True}


# ─── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Agent web UI → http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
