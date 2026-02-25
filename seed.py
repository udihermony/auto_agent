#!/usr/bin/env python3
"""
Self-evolving AI agent — minimal seed.

Usage:
    python seed.py              # persistent agent, waits for tasks via web UI
    python seed.py --sub ID     # sub-agent mode (spawned by parent agent)

Environment:
    ANTHROPIC_API_KEY   required for default Anthropic provider
    LLM_PROVIDER        'anthropic' (default) or 'openai' (for Ollama, LM Studio, etc.)
    LLM_BASE_URL        base URL when using openai provider (default: http://localhost:11434/v1)
    MODEL               model name (default: claude-opus-4-6)
"""

import sys, os, json, time, uuid, shutil, subprocess, re
from pathlib import Path
from datetime import datetime

# ─── Project paths ─────────────────────────────────────────────────────────────
SEED_FILE = Path(__file__).resolve()   # always points to seed.py itself

def _resolve_root() -> Path:
    """Support --root PATH so multiple agent instances can share one seed.py."""
    args = sys.argv[1:]
    if "--root" in args:
        idx = args.index("--root")
        if idx + 1 < len(args):
            return Path(args[idx + 1]).resolve()
    return SEED_FILE.parent

ROOT = _resolve_root()

# ─── Load .env file (if present) ───────────────────────────────────────────────
def _load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        # Strip optional surrounding quotes from value
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)

_load_env()
TOOLS_DIR   = ROOT / "tools"
MEMORY_DIR  = ROOT / "memory"
STATE_DIR   = ROOT / "state"
SANDBOX_DIR = ROOT / "sandbox"
AGENTS_DIR  = ROOT / "agents"
GENOME_DIR  = ROOT / "genome"

for _d in [TOOLS_DIR, MEMORY_DIR / "sessions", STATE_DIR, SANDBOX_DIR, AGENTS_DIR, GENOME_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

REGISTRY   = TOOLS_DIR / "__registry__.json"
SEMANTIC   = MEMORY_DIR / "semantic.md"
PAUSE_FLAG = STATE_DIR / "pause"
INBOX      = STATE_DIR / "inbox.json"
STATUS     = STATE_DIR / "status.json"

# Active stream files — write_log() writes to all of them simultaneously.
# Main agent:   [state/stream.jsonl, memory/sessions/{id}/stream.jsonl]
# Sub-agent:    [agents/{id}/stream.jsonl]
_streams: list[Path] = [STATE_DIR / "stream.jsonl"]

PROMPT_FILE = STATE_DIR / "agent_prompt.txt"


# ─── LLM abstraction ───────────────────────────────────────────────────────────
class LLM:
    def __init__(self):
        prov = os.getenv("LLM_PROVIDER", "anthropic")
        self.model = os.getenv("MODEL", "claude-opus-4-6")
        if prov == "anthropic":
            from anthropic import Anthropic
            self._c = Anthropic()
            self._mode = "anthropic"
        else:
            from openai import OpenAI
            self._c = OpenAI(
                base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
                api_key=os.getenv("LLM_API_KEY", "local"),
            )
            self._mode = "openai"

    def call(self, system: str, messages: list) -> str:
        if self._mode == "anthropic":
            r = self._c.messages.create(
                model=self.model, max_tokens=4096, system=system, messages=messages
            )
            return r.content[0].text
        r = self._c.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}] + messages,
            max_tokens=4096,
        )
        return r.choices[0].message.content


_llm = LLM()


# ─── Logging / state ───────────────────────────────────────────────────────────
def write_log(type: str, content: str, **extra):
    entry = {
        "ts": datetime.now().isoformat(),
        "type": type,
        "content": str(content)[:600],
        **extra,
    }
    line = json.dumps(entry, ensure_ascii=False)
    for p in _streams:
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    print(f"[{type:<20}] {str(content)[:100]}")


def set_status(**kw):
    cur = {}
    if STATUS.exists():
        try:
            cur = json.loads(STATUS.read_text(encoding="utf-8"))
        except Exception:
            pass
    cur.update(kw)
    cur["ts"] = datetime.now().isoformat()
    STATUS.write_text(json.dumps(cur, indent=2), encoding="utf-8")


def _interruptible_sleep(seconds: float, interval: float = 0.1):
    """Sleep in small chunks so Ctrl+C is caught promptly on Windows."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(interval)


def check_inbox() -> dict | None:
    """Block while paused, then return inbox message if present."""
    if PAUSE_FLAG.exists():
        write_log("paused", "Paused — resume via web UI")
        set_status(phase="paused")
        while PAUSE_FLAG.exists():
            _interruptible_sleep(0.5)
        write_log("resumed", "Resuming")
    if INBOX.exists():
        try:
            msg = json.loads(INBOX.read_text(encoding="utf-8"))
            INBOX.unlink()
            write_log("human_msg", msg.get("content", ""))
            return msg
        except Exception:
            pass
    return None


# ─── Tool registry ─────────────────────────────────────────────────────────────
def get_registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_registry(r: dict):
    REGISTRY.write_text(json.dumps(r, indent=2), encoding="utf-8")


def registry_summary() -> str:
    r = get_registry()
    if not r:
        return "  (no tools yet)"
    lines = []
    for name, info in r.items():
        if info.get("status") == "deprecated":
            continue
        score = info.get("score", 1.0)
        desc = info.get("description", "")
        note = f" ⚠ {info['improvement_note']}" if info.get("improvement_note") else ""
        lines.append(f"  - {name} [score:{score:.2f}]: {desc}{note}")
    return "\n".join(lines) if lines else "  (no active tools)"


def register_tool(name: str, desc: str, code: str) -> dict:
    path = TOOLS_DIR / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    r = get_registry()
    r[name] = {
        "description": desc,
        "path": str(path),
        "created": datetime.now().isoformat(),
        "uses": 0,
        "failures": 0,
        "score": 1.0,
        "status": "active",
    }
    save_registry(r)
    write_log("tool_written", f"{name}: {desc}")
    return {"ok": True, "path": str(path)}


def call_tool(name: str, args: dict) -> dict:
    r = get_registry()
    if name not in r:
        available = [k for k, v in r.items() if v.get("status") != "deprecated"]
        return {"error": f"Tool '{name}' not found. Available: {available}"}
    path = Path(r[name]["path"])
    if not path.exists():
        return {"error": f"Tool file missing: {path}"}

    # Build a small runner script so the tool runs in the sandbox
    code = f"""
import sys, json, os
sys.path.insert(0, r'{TOOLS_DIR}')
sys.path.insert(0, r'{ROOT}')
os.environ['AGENT_ROOT'] = r'{ROOT}'
import importlib.util as ilu
spec = ilu.spec_from_file_location("tool", r'{path}')
mod = ilu.module_from_spec(spec)
spec.loader.exec_module(mod)
result = mod.run(**{json.dumps(args)})
print(json.dumps({{"result": result}}))
"""
    res = exec_safe(code, timeout=60)

    # Update score
    reg = get_registry()
    if name in reg:
        reg[name]["uses"] = reg[name].get("uses", 0) + 1
        if not res["success"]:
            reg[name]["failures"] = reg[name].get("failures", 0) + 1
        u = reg[name]["uses"]
        f = reg[name]["failures"]
        reg[name]["score"] = round(1.0 - f / max(u, 1), 2)
        save_registry(reg)

    write_log("tool_call", f"{name}({str(args)[:60]}) → {'✓' if res['success'] else '✗'}")

    if res["success"] and res["stdout"]:
        try:
            parsed = json.loads(res["stdout"])
            return parsed.get("result", parsed)
        except Exception:
            return {"output": res["stdout"]}
    return res


# ─── Sandbox executor ──────────────────────────────────────────────────────────
def exec_safe(code: str, timeout: int = 30) -> dict:
    run_dir = SANDBOX_DIR / str(uuid.uuid4())[:8]
    run_dir.mkdir(parents=True)
    script = run_dir / "run.py"
    script.write_text(code, encoding="utf-8")
    env = {
        **os.environ,
        "AGENT_ROOT": str(ROOT),
        "PYTHONPATH": str(ROOT) + os.pathsep + str(TOOLS_DIR),
    }
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(run_dir),
            env=env,
        )
        return {
            "stdout": r.stdout[:3000],
            "stderr": r.stderr[:500],
            "exit_code": r.returncode,
            "success": r.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timeout after {timeout}s", "exit_code": -1, "success": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1, "success": False}
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# ─── Memory ────────────────────────────────────────────────────────────────────
def read_semantic() -> str:
    return SEMANTIC.read_text(encoding="utf-8") if SEMANTIC.exists() else "(empty — no lessons yet)"


def add_to_memory(content: str):
    ts = datetime.now().strftime("%Y-%m-%d")
    with open(SEMANTIC, "a", encoding="utf-8") as f:
        f.write(f"\n### {ts}\n{content}\n")
    write_log("memory_write", content[:120])


# ─── Sub-agents ────────────────────────────────────────────────────────────────
def spawn_sub(task: str, agent_id: str) -> str:
    d = AGENTS_DIR / agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.json").write_text(json.dumps({"task": task}), encoding="utf-8")
    (d / "status.json").write_text(json.dumps({"phase": "starting"}), encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(SEED_FILE), "--sub", agent_id, "--root", str(ROOT)],
        stdout=open(d / "stdout.log", "w"),
        stderr=open(d / "stderr.log", "w"),
        cwd=str(SEED_FILE.parent),
    )
    write_log("spawn_sub", f"Sub-agent {agent_id}: {task[:80]}", agent_id=agent_id)
    return agent_id


def collect_sub(agent_id: str, timeout: int = 300) -> dict:
    rf = AGENTS_DIR / agent_id / "result.json"
    write_log("collect_sub", f"Waiting for sub-agent {agent_id} (timeout {timeout}s)…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if rf.exists():
            result = json.loads(rf.read_text(encoding="utf-8"))
            write_log("sub_done", f"Sub-agent {agent_id} finished", agent_id=agent_id)
            return result
        time.sleep(2)
    write_log("sub_timeout", f"Sub-agent {agent_id} timed out", agent_id=agent_id)
    return {"error": "timeout", "agent_id": agent_id}


# ─── Response parser ───────────────────────────────────────────────────────────
def parse_action(text: str) -> dict:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {"thinking": text, "action": "think", "content": text}


# ─── Agent prompt (editable via web UI) ───────────────────────────────────────
def get_agent_prompt() -> str:
    """Load prompt from file so UI edits take effect without restarting."""
    if PROMPT_FILE.exists():
        return PROMPT_FILE.read_text(encoding="utf-8")
    return AGENT_SYSTEM


# ─── System prompts ────────────────────────────────────────────────────────────
AGENT_SYSTEM = """You are a self-evolving AI agent running on Windows. You complete tasks by writing and reusing persistent Python tools. Each task runs in a loop — you act, observe results, and decide the next step.

## Project layout  (AGENT_ROOT env var = project root)
  tools/                  Python tool files you write and reuse across tasks
  tools/__registry__.json tool index with usage scores
  memory/semantic.md      distilled lessons from all past tasks — read at task start
  memory/sessions/        archived per-task logs
  agents/{id}/            sub-agent working directories

## Response format
Respond ONLY with a single JSON object. No prose outside the JSON.

## Actions

exec — run Python code in an isolated sandbox subprocess
{"thinking":"…","action":"exec","code":"print('hello')","description":"what this does"}
  • AGENT_ROOT env var = project root; PYTHONPATH includes tools/ and root
  • stdout/stderr captured; default timeout 30s
  • Always use sys.executable instead of bare 'python'
  • Install packages: subprocess.run([sys.executable,"-m","pip","install","pkg"])

write_tool — write a reusable tool and register it
{"thinking":"…","action":"write_tool","name":"snake_name","description":"what it does","code":"def run(**kwargs):\\n    …\\n    return value"}
  • Tool MUST expose run(**kwargs) returning a JSON-serialisable value
  • Test logic with exec first, then promote to write_tool
  • Check existing tools before writing a new one

use_tool — call a registered tool by name
{"thinking":"…","action":"use_tool","name":"tool_name","args":{"key":"val"}}

spawn_sub — spawn a sub-agent for a parallel subtask
{"thinking":"…","action":"spawn_sub","task":"specific subtask description","agent_id":"short_id"}
  • Sub-agents share your tools/ directory (read access)
  • Good when task has 3+ clearly independent parts

collect_sub — wait for sub-agent to finish and get its result
{"thinking":"…","action":"collect_sub","agent_id":"the_id"}

remember — append a reusable lesson to memory/semantic.md
{"thinking":"…","action":"remember","content":"the lesson"}
  • Write general truths, not task-specific notes
  • Examples: "requests needs timeout=10 on Windows", "tool X fails on UNC paths"

done — signal task completion
{"thinking":"…","action":"done","result":"description of what was achieved"}

## Rules
1. At task start, review the memory/semantic.md section in context — it has hard-won lessons
2. Check the tools list — reuse before rewriting
3. exec to test logic → write_tool to make it permanent
4. run(**kwargs) must return a JSON-serialisable value (not None for output tools)
5. Windows: use pathlib.Path, sys.executable; avoid shell=True in subprocess calls
6. If a tool has score < 0.5, rewrite it rather than using it
7. Human messages marked [Human] in context are high-priority steering — adjust immediately
8. Spawn sub-agents for genuinely parallel work; collect results before signalling done"""

REFLECT_SYSTEM = """You are reviewing a completed task session to extract reusable lessons.

Return ONLY this JSON (no other text):
{
  "lessons": ["concise reusable lesson", "…"],
  "tool_improvements": [{"name":"tool_name","issue":"what went wrong","fix":"how to improve it"}]
}

Focus on: Windows-specific quirks, reusable patterns discovered, tool failure root causes, parallelism opportunities missed, anything that would help future tasks start faster."""

# Write default prompt to file as soon as the module loads so the web UI
# can display it even before seed.py has fully started.
if not PROMPT_FILE.exists():
    PROMPT_FILE.write_text(AGENT_SYSTEM, encoding="utf-8")


# ─── Task loop ─────────────────────────────────────────────────────────────────
MAX_STEPS    = 60
MAX_MESSAGES = 30


def run_task(task: str, session_dir: Path) -> str:
    write_log("task_start", task)

    initial = (
        f"## Accumulated knowledge\n{read_semantic()}\n\n"
        f"## Available tools\n{registry_summary()}\n\n"
        f"## Task\n{task}\n\n"
        "Review existing tools and knowledge before acting. "
        "Test code with exec before writing tools."
    )
    messages = [{"role": "user", "content": initial}]

    for step in range(MAX_STEPS):
        # Human intervention checkpoint
        inbox = check_inbox()
        if inbox:
            if inbox.get("type") == "abort":
                write_log("abort", inbox.get("content", "Aborted by human"))
                return "aborted"
            messages.append({"role": "user", "content": f"[Human]: {inbox['content']}"})

        set_status(phase="working", step=step)

        # Trim message history to avoid huge contexts
        if len(messages) > MAX_MESSAGES:
            messages = messages[:1] + messages[-(MAX_MESSAGES - 1):]

        write_log("llm_call", f"Step {step + 1}/{MAX_STEPS} — calling LLM…")
        try:
            raw = _llm.call(get_agent_prompt(), messages)
        except Exception as e:
            write_log("llm_error", str(e))
            time.sleep(5)
            continue

        action = parse_action(raw)
        if action.get("thinking"):
            write_log("thinking", action["thinking"])

        act = action.get("action", "think")
        result: dict = {}

        if act == "exec":
            desc = action.get("description", action.get("code", "")[:80])
            write_log("exec", desc)
            result = exec_safe(action.get("code", ""))
            out = result["stdout"] or result["stderr"]
            write_log("exec_result", f"exit={result['exit_code']} | {out[:200]}")

        elif act == "write_tool":
            result = register_tool(
                action.get("name", "unnamed"),
                action.get("description", ""),
                action.get("code", ""),
            )

        elif act == "use_tool":
            result = call_tool(action.get("name", ""), action.get("args", {}))

        elif act == "spawn_sub":
            aid = action.get("agent_id", str(uuid.uuid4())[:8])
            spawn_sub(action.get("task", ""), aid)
            result = {"spawned": aid, "msg": "Use collect_sub to wait for results"}

        elif act == "collect_sub":
            result = collect_sub(action.get("agent_id", ""))

        elif act == "remember":
            add_to_memory(action.get("content", ""))
            result = {"ok": True}

        elif act == "think":
            result = {"ok": "continue"}

        elif act == "done":
            final = action.get("result", "Task complete.")
            write_log("task_done", final)
            (session_dir / "result.json").write_text(
                json.dumps({"result": final, "steps": step + 1, "ts": datetime.now().isoformat()}),
                encoding="utf-8",
            )
            return final

        else:
            write_log("unknown_action", f"Unknown action: {act!r}")
            result = {"error": f"Unknown action '{act}'. Valid: exec, write_tool, use_tool, spawn_sub, collect_sub, remember, done"}

        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"result: {json.dumps(result, default=str)[:1500]}"})

    write_log("max_steps", "Reached step limit without completion")
    return "max_steps_reached"


# ─── Reflection ────────────────────────────────────────────────────────────────
def reflect(session_dir: Path):
    write_log("reflecting", "Distilling lessons from completed session…")
    set_status(phase="reflecting")
    sf = session_dir / "stream.jsonl"
    if not sf.exists():
        return
    lines = sf.read_text(encoding="utf-8").splitlines()[-80:]
    try:
        raw = _llm.call(
            REFLECT_SYSTEM,
            [{"role": "user", "content":
                f"Session log (last 80 events):\n{chr(10).join(lines)}\n\n"
                f"Current semantic memory:\n{read_semantic()[:800]}"}],
        )
        r = parse_action(raw)
        lessons = r.get("lessons", [])
        if lessons:
            add_to_memory("### Session lessons\n" + "\n".join(f"- {l}" for l in lessons))
        for imp in r.get("tool_improvements", []):
            reg = get_registry()
            if imp.get("name") in reg:
                reg[imp["name"]]["improvement_note"] = imp.get("fix", "")
                save_registry(reg)
        write_log("reflect_done", f"{len(lessons)} lessons, {len(r.get('tool_improvements',[]))} tool notes")
    except Exception as e:
        write_log("reflect_error", str(e))


# ─── Main persistent loop ──────────────────────────────────────────────────────
def main():
    sessions_dir = MEMORY_DIR / "sessions"
    n = len(list(sessions_dir.iterdir())) if sessions_dir.exists() else 0
    set_status(phase="idle", sessions=n, pid=os.getpid())
    write_log("agent_start", f"Agent ready. {n} past sessions. Post a task via the web UI.")

    while True:
        set_status(phase="idle")
        write_log("idle", "Waiting for task via web UI (http://localhost:8000)…")

        # Block until inbox delivers a task
        task = None
        while task is None:
            inbox = check_inbox()
            if inbox and inbox.get("type") in ("task", "steer"):
                task = inbox["content"]
            else:
                _interruptible_sleep(1)

        # Create session directory and attach its stream
        n += 1
        sid = f"{n:04d}_{datetime.now().strftime('%H%M%S')}"
        sdir = sessions_dir / sid
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "task.txt").write_text(task, encoding="utf-8")

        _streams.append(sdir / "stream.jsonl")
        set_status(phase="working", session=sid, task=task[:80], sessions=n)

        run_task(task, sdir)
        reflect(sdir)

        _streams.pop()
        set_status(phase="idle", session=sid, sessions=n)


# ─── Sub-agent entry ───────────────────────────────────────────────────────────
def run_as_subagent(agent_id: str):
    d = AGENTS_DIR / agent_id
    task = json.loads((d / "task.json").read_text(encoding="utf-8"))["task"]

    # Sub-agents write only to their own stream, not the parent's
    _streams.clear()
    _streams.append(d / "stream.jsonl")

    (d / "status.json").write_text(json.dumps({"phase": "working", "task": task[:80]}), encoding="utf-8")
    write_log("subagent_start", f"{agent_id}: {task[:80]}", agent_id=agent_id)

    result = run_task(task, d)

    if not (d / "result.json").exists():
        (d / "result.json").write_text(json.dumps({"result": result}), encoding="utf-8")
    (d / "status.json").write_text(json.dumps({"phase": "done"}), encoding="utf-8")
    write_log("subagent_done", f"{agent_id} complete", agent_id=agent_id)


# ─── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        if "--sub" in sys.argv:
            idx = sys.argv.index("--sub")
            run_as_subagent(sys.argv[idx + 1])
        else:
            main()
    except KeyboardInterrupt:
        print("\nAgent stopped.")
        set_status(phase="stopped")
        sys.exit(0)
