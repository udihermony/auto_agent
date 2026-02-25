#!/usr/bin/env python3
"""
Local web server for observing and steering the agent.

Usage:
    python server.py

Then open http://localhost:8000
"""

import json, asyncio, shutil, subprocess, sys, re, ctypes
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn

ROOT          = Path(__file__).parent.resolve()
INSTANCES_DIR = ROOT / "instances"
INSTANCES_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Agent UI")

# Running agent processes: {instance_id: Popen}
_procs: dict[str, subprocess.Popen] = {}


# ─── Instance helpers ──────────────────────────────────────────────────────────
def instance_root(iid: str) -> Path:
    """Return the root directory for an instance. 'default' maps to ROOT."""
    if iid == "default":
        return ROOT
    return INSTANCES_DIR / iid


def pid_alive(pid: int) -> bool:
    """Windows-compatible process existence check."""
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def instance_running(iid: str) -> bool:
    proc = _procs.get(iid)
    if proc and proc.poll() is None:
        return True
    # Fall back to checking PID stored in status.json
    try:
        sf = instance_root(iid) / "state" / "status.json"
        pid = json.loads(sf.read_text(encoding="utf-8")).get("pid")
        return bool(pid and pid_alive(pid))
    except Exception:
        return False


def list_instances() -> list:
    result = [{"id": "default", "root": str(ROOT)}]
    if INSTANCES_DIR.exists():
        for d in sorted(INSTANCES_DIR.iterdir()):
            if d.is_dir():
                result.append({"id": d.name, "root": str(d)})
    return result


def rj(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


# ─── UI ────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return (ROOT / "web" / "index.html").read_text(encoding="utf-8")


# ─── Instance management ───────────────────────────────────────────────────────
@app.get("/instances")
async def get_instances():
    result = []
    for inst in list_instances():
        iid  = inst["id"]
        idir = Path(inst["root"])
        status = rj(idir / "state" / "status.json", {})
        result.append({
            "id":      iid,
            "phase":   status.get("phase", "stopped"),
            "task":    status.get("task", ""),
            "running": instance_running(iid),
        })
    return result


@app.post("/instances")
async def create_instance(body: dict):
    name = body.get("name", "").strip().lower().replace(" ", "_")
    if not name or name == "default":
        return {"ok": False, "error": "Invalid name — cannot be empty or 'default'"}

    idir = INSTANCES_DIR / name
    if idir.exists():
        return {"ok": False, "error": f"Instance '{name}' already exists"}

    idir.mkdir(parents=True)

    # Carry over tools (copy registry + all tool files)
    if body.get("carry_tools"):
        src = ROOT / "tools"
        if src.exists():
            shutil.copytree(src, idir / "tools")

    # Carry over memory (semantic knowledge only, not full session history)
    if body.get("carry_memory"):
        src = ROOT / "memory" / "semantic.md"
        if src.exists():
            (idir / "memory").mkdir(parents=True, exist_ok=True)
            shutil.copy(src, idir / "memory" / "semantic.md")

    # Start the agent process
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "seed.py"), "--root", str(idir)],
        cwd=str(ROOT),
    )
    _procs[name] = proc
    return {"ok": True, "id": name}


@app.post("/instances/{iid}/start")
async def start_instance(iid: str):
    idir = instance_root(iid)
    if not idir.exists():
        return {"ok": False, "error": "Instance not found"}
    if instance_running(iid):
        return {"ok": False, "error": "Already running"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "seed.py"), "--root", str(idir)],
        cwd=str(ROOT),
    )
    _procs[iid] = proc
    return {"ok": True}


@app.delete("/instances/{iid}")
async def delete_instance(iid: str):
    if iid == "default":
        return {"ok": False, "error": "Cannot delete the default instance"}
    idir = INSTANCES_DIR / iid
    if not idir.exists():
        return {"ok": False, "error": "Not found"}
    # Stop process if running
    proc = _procs.pop(iid, None)
    if proc and proc.poll() is None:
        proc.terminate()
    shutil.rmtree(idir, ignore_errors=True)
    return {"ok": True}


# ─── Live stream (SSE) ─────────────────────────────────────────────────────────
@app.get("/stream")
async def stream_events(request: Request, instance: str = "default"):
    stream_file = instance_root(instance) / "state" / "stream.jsonl"
    stream_file.parent.mkdir(parents=True, exist_ok=True)
    stream_file.touch()

    async def generator():
        try:
            content = stream_file.read_bytes()
            lines = content.decode("utf-8", errors="replace").splitlines()
            for line in lines[-300:]:
                if line.strip():
                    yield f"data: {line.strip()}\n\n"
            pos = len(content)
        except Exception:
            pos = 0

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
async def get_state(instance: str = "default"):
    ir = instance_root(instance)
    return {
        "status":  rj(ir / "state"  / "status.json", {}),
        "tools":   rj(ir / "tools"  / "__registry__.json", {}),
        "memory":  (ir / "memory" / "semantic.md").read_text(encoding="utf-8")
                   if (ir / "memory" / "semantic.md").exists() else "",
        "paused":  (ir / "state" / "pause").exists(),
    }


# ─── Session history ───────────────────────────────────────────────────────────
@app.get("/sessions")
async def get_sessions(instance: str = "default"):
    sessions_dir = instance_root(instance) / "memory" / "sessions"
    if not sessions_dir.exists():
        return []
    result = []
    for d in sorted(sessions_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        result.append({
            "id":     d.name,
            "task":   (d / "task.txt").read_text(encoding="utf-8").strip()
                      if (d / "task.txt").exists() else "",
            "done":   (d / "result.json").exists(),
            "result": rj(d / "result.json"),
        })
    return result


@app.get("/sessions/{session_id}/stream")
async def session_stream(session_id: str, instance: str = "default"):
    path = instance_root(instance) / "memory" / "sessions" / session_id / "stream.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ─── Sub-agents ────────────────────────────────────────────────────────────────
@app.get("/agents")
async def get_agents(instance: str = "default"):
    agents_dir = instance_root(instance) / "agents"
    if not agents_dir.exists():
        return []
    result = []
    for d in sorted(agents_dir.iterdir()):
        if not d.is_dir():
            continue
        result.append({
            "id":     d.name,
            "task":   (rj(d / "task.json") or {}).get("task", ""),
            "status": rj(d / "status.json") or {},
            "done":   (d / "result.json").exists(),
            "result": rj(d / "result.json"),
        })
    return result


@app.get("/agents/{agent_id}/stream")
async def agent_stream(agent_id: str, instance: str = "default"):
    path = instance_root(instance) / "agents" / agent_id / "stream.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ─── Agent prompt ──────────────────────────────────────────────────────────────
@app.get("/prompt")
async def get_prompt(instance: str = "default"):
    p = instance_root(instance) / "state" / "agent_prompt.txt"
    if p.exists():
        return {"content": p.read_text(encoding="utf-8"), "saved": True}
    try:
        seed_text = (ROOT / "seed.py").read_text(encoding="utf-8")
        m = re.search(r'AGENT_SYSTEM\s*=\s*"""([\s\S]*?)"""', seed_text)
        if m:
            return {"content": m.group(1), "saved": False}
    except Exception:
        pass
    return {"content": "", "saved": False}


@app.post("/prompt")
async def save_prompt(body: dict):
    instance = body.get("instance", "default")
    content  = body.get("content", "")
    if not content.strip():
        return {"ok": False, "error": "Prompt cannot be empty"}
    p = instance_root(instance) / "state" / "agent_prompt.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True}


@app.post("/prompt/reset")
async def reset_prompt(body: dict = {}):
    instance = body.get("instance", "default")
    p = instance_root(instance) / "state" / "agent_prompt.txt"
    if p.exists():
        p.unlink()
    return {"ok": True}


# ─── Control endpoints ─────────────────────────────────────────────────────────
@app.post("/task")
async def post_task(body: dict):
    ir = instance_root(body.get("instance", "default"))
    ir.mkdir(parents=True, exist_ok=True)
    (ir / "state" / "inbox.json").write_text(
        json.dumps({"type": "task", "content": body.get("task", "")}), encoding="utf-8"
    )
    return {"ok": True}


@app.post("/steer")
async def steer(body: dict):
    ir = instance_root(body.get("instance", "default"))
    (ir / "state" / "inbox.json").write_text(
        json.dumps({"type": "steer", "content": body.get("message", "")}), encoding="utf-8"
    )
    return {"ok": True}


@app.post("/abort")
async def abort(body: dict):
    ir = instance_root(body.get("instance", "default"))
    (ir / "state" / "inbox.json").write_text(
        json.dumps({"type": "abort", "content": body.get("message", "Aborted")}), encoding="utf-8"
    )
    return {"ok": True}


@app.post("/pause")
async def pause(body: dict = {}):
    (instance_root(body.get("instance", "default")) / "state" / "pause").touch()
    return {"ok": True}


@app.post("/resume")
async def resume(body: dict = {}):
    p = instance_root(body.get("instance", "default")) / "state" / "pause"
    if p.exists():
        p.unlink()
    return {"ok": True}


# ─── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Agent web UI → http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
