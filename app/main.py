import json
import os
import base64
import uuid
import threading
from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Request
from e2b import Sandbox

app = FastAPI()


# ──────────────────────────────────────────────
# Auth middleware
# ──────────────────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    token = request.headers.get("Authorization")
    expected = f"Bearer {os.getenv('API_AUTH_TOKEN', '')}"
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await call_next(request)


# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
system_prompt = """
GitHub PAT is already set in the environment GITHUB_PAT. The repository is already cloned in the sandbox and the working directory is the repository root.
"""

sandbox_template = os.getenv("E2B_SANDBOX_TEMPLATE", "claude-code-dev")
sandbox_timeout = 60 * 60  # 1 hour

# Job tracking: job_id -> { status, sandbox_id, result, error, session_id }
jobs = {}

# claude session id -> sandbox id
session_sandbox_map = {}

# sandbox id -> Sandbox object (keep alive for file extraction)
active_sandboxes = {}


# ──────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────
class ClaudePrompt(BaseModel):
    prompt: str
    repo: Optional[str] = None


class FileInfo(BaseModel):
    path: str
    name: str
    size: int
    extension: str


# ──────────────────────────────────────────────
# Background worker: runs Claude Code in sandbox
# ──────────────────────────────────────────────
def run_agent_in_background(job_id: str, prompt_text: str, repo: Optional[str], session: Optional[str]):
    try:
        if session is None:
            sandbox = Sandbox.create(
                template=sandbox_template,
                timeout=sandbox_timeout,
                envs={
                    "GITHUB_PAT": os.getenv("GITHUB_PAT", ""),
                    "CONTEXT7_API_KEY": os.getenv("CONTEXT7_API_KEY", ""),
                    "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
                },
            )
            if repo:
                sandbox.commands.run(
                    f"git clone {repo} && cd {repo.split('/')[-1]}"
                )
        else:
            sandbox = Sandbox.connect(sandbox_id=session_sandbox_map[session])

        # Store sandbox reference
        active_sandboxes[sandbox.sandbox_id] = sandbox
        jobs[job_id]["sandbox_id"] = sandbox.sandbox_id

        cmd = "claude"
        claude_args = [
            "-p",
            "--dangerously-skip-permissions",
            "--output-format",
            "json",
            "--append-system-prompt",
            f'"{system_prompt}"',
        ]

        if session:
            claude_args.append("--resume")
            claude_args.append(session)

        response = sandbox.commands.run(
            f"echo {json.dumps(prompt_text)} | {cmd} {' '.join(claude_args)}",
            timeout=0,
        )

        if response.stderr:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = response.stderr
            return

        claude_response = json.loads(response.stdout)
        session_sandbox_map[claude_response["session_id"]] = sandbox.sandbox_id

        claude_response["sandbox_id"] = sandbox.sandbox_id

        jobs[job_id]["status"] = "complete"
        jobs[job_id]["result"] = claude_response
        jobs[job_id]["session_id"] = claude_response.get("session_id")

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ──────────────────────────────────────────────
# POST /chat — ASYNC: returns immediately with job_id
# ──────────────────────────────────────────────
@app.post("/chat/{session}")
@app.post("/chat")
def prompt(prompt: ClaudePrompt, session: Optional[str] = None):
    job_id = str(uuid.uuid4())

    jobs[job_id] = {
        "status": "processing",
        "sandbox_id": None,
        "result": None,
        "error": None,
        "session_id": None,
    }

    thread = threading.Thread(
        target=run_agent_in_background,
        args=(job_id, prompt.prompt, prompt.repo, session),
        daemon=True,
    )
    thread.start()

    return {
        "job_id": job_id,
        "status": "processing",
        "message": "Agent started. Poll GET /result/{job_id} for status.",
    }


# ──────────────────────────────────────────────
# GET /result/{job_id} — Poll for agent completion
# ──────────────────────────────────────────────
@app.get("/result/{job_id}")
def get_result(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]

    return {
        "job_id": job_id,
        "status": job["status"],
        "sandbox_id": job["sandbox_id"],
        "result": job["result"],
        "error": job["error"],
        "session_id": job["session_id"],
    }


# ──────────────────────────────────────────────
# GET /files/{sandbox_id} — List files in sandbox
# ──────────────────────────────────────────────
@app.get("/files/{sandbox_id}")
def list_files(sandbox_id: str):
    sandbox = _get_sandbox(sandbox_id)

    result = sandbox.commands.run(
        'find /home/user -type f '
        '-not -path "*/\\.*" '
        '-not -path "*/node_modules/*" '
        '-not -path "*/__pycache__/*" '
        '-not -name "*.pyc" '
        '2>/dev/null | head -100',
        timeout=30,
    )

    if not result.stdout or not result.stdout.strip():
        return {"sandbox_id": sandbox_id, "files": []}

    files = []
    for filepath in result.stdout.strip().split("\n"):
        filepath = filepath.strip()
        if not filepath:
            continue

        size_result = sandbox.commands.run(f'stat -c %s "{filepath}" 2>/dev/null', timeout=5)
        size = int(size_result.stdout.strip()) if size_result.stdout and size_result.stdout.strip().isdigit() else 0

        name = filepath.split("/")[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

        files.append(FileInfo(
            path=filepath,
            name=name,
            size=size,
            extension=ext,
        ))

    return {"sandbox_id": sandbox_id, "files": [f.dict() for f in files]}


# ──────────────────────────────────────────────
# GET /files/{sandbox_id}/download — Single file as base64
# ──────────────────────────────────────────────
@app.get("/files/{sandbox_id}/download")
def download_file(sandbox_id: str, path: str):
    sandbox = _get_sandbox(sandbox_id)

    result = sandbox.commands.run(
        f'base64 -w 0 "{path}" 2>/dev/null',
        timeout=30,
    )

    if not result.stdout or result.exit_code != 0:
        raise HTTPException(status_code=404, detail=f"File not found or unreadable: {path}")

    name = path.split("/")[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    return {
        "path": path,
        "name": name,
        "extension": ext,
        "content_base64": result.stdout.strip(),
        "mime_type": _get_mime_type(ext),
    }


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def _get_sandbox(sandbox_id: str) -> Sandbox:
    if sandbox_id in active_sandboxes:
        return active_sandboxes[sandbox_id]
    try:
        sandbox = Sandbox.connect(sandbox_id=sandbox_id)
        active_sandboxes[sandbox_id] = sandbox
        return sandbox
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Sandbox not found or expired: {str(e)}")


def _get_mime_type(ext: str) -> str:
    mime_map = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "ppt": "application/vnd.ms-powerpoint",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel",
        "csv": "text/csv",
        "html": "text/html",
        "htm": "text/html",
        "css": "text/css",
        "js": "application/javascript",
        "ts": "application/typescript",
        "jsx": "application/javascript",
        "tsx": "application/typescript",
        "json": "application/json",
        "md": "text/markdown",
        "txt": "text/plain",
        "yaml": "text/yaml",
        "yml": "text/yaml",
        "py": "text/x-python",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "webp": "image/webp",
        "zip": "application/zip",
        "tar.gz": "application/gzip",
    }
    return mime_map.get(ext, "application/octet-stream")


# ──────────────────────────────────────────────
# Health check
# ──────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}
