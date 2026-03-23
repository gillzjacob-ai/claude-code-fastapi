import json
import os
import base64
import httpx
from typing import Optional, List
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


class SupabaseUploadRequest(BaseModel):
    supabase_url: str
    supabase_service_key: str
    bucket: str = "agent-files"
    session_id: Optional[str] = None
    # Optional: only upload specific files (paths relative to /home/user)
    file_paths: Optional[List[str]] = None


class FileInfo(BaseModel):
    path: str
    name: str
    size: int
    extension: str


# ──────────────────────────────────────────────
# Existing: /chat endpoint (modified to return sandbox_id)
# ──────────────────────────────────────────────
@app.post("/chat/{session}")
@app.post("/chat")
def prompt(prompt: ClaudePrompt, session: Optional[str] = None):
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
        if prompt.repo:
            sandbox.commands.run(
                f"git clone {prompt.repo} && cd {prompt.repo.split('/')[-1]}"
            )
    else:
        sandbox = Sandbox.connect(sandbox_id=session_sandbox_map[session])

    # Store sandbox reference for file extraction later
    active_sandboxes[sandbox.sandbox_id] = sandbox

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
        f"echo {json.dumps(prompt.prompt)} | {cmd} {' '.join(claude_args)}",
        timeout=0,
    )

    if response.stderr:
        raise HTTPException(status_code=500, detail=response.stderr)

    claude_response = json.loads(response.stdout)
    session_sandbox_map[claude_response["session_id"]] = sandbox.sandbox_id

    # Add sandbox_id to response so caller can extract files
    claude_response["sandbox_id"] = sandbox.sandbox_id

    return claude_response


# ──────────────────────────────────────────────
# NEW: List files created in a sandbox
# ──────────────────────────────────────────────
@app.get("/files/{sandbox_id}")
def list_files(sandbox_id: str):
    """
    Lists all non-system files created in the sandbox under /home/user.
    Filters out hidden files, node_modules, etc.
    Returns file paths, names, sizes, and extensions.
    """
    sandbox = _get_sandbox(sandbox_id)

    # Find all files created in the sandbox working directory
    # Exclude hidden dirs, node_modules, and common system files
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

        # Get file size
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
# NEW: Download a single file as base64
# ──────────────────────────────────────────────
@app.get("/files/{sandbox_id}/download")
def download_file(sandbox_id: str, path: str):
    """
    Reads a single file from the sandbox and returns it as base64.
    Query param: ?path=/home/user/output.pdf
    """
    sandbox = _get_sandbox(sandbox_id)

    # Read file as base64 from sandbox
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
# NEW: Upload sandbox files to Supabase Storage
# ──────────────────────────────────────────────
@app.post("/files/{sandbox_id}/upload-to-supabase")
async def upload_to_supabase(sandbox_id: str, req: SupabaseUploadRequest):
    """
    Reads files from the sandbox and uploads them to Supabase Storage.
    Returns public download URLs for each file.

    This is the main endpoint the Edge Function calls after an agent completes.
    """
    sandbox = _get_sandbox(sandbox_id)

    # If specific files requested, use those. Otherwise discover all files.
    if req.file_paths:
        file_paths = req.file_paths
    else:
        # Discover deliverable files (skip system/config files)
        result = sandbox.commands.run(
            'find /home/user -type f '
            '-not -path "*/\\.*" '
            '-not -path "*/node_modules/*" '
            '-not -path "*/__pycache__/*" '
            '-not -name "*.pyc" '
            '-not -name "*.log" '
            '\\( '
            '-name "*.pdf" -o -name "*.docx" -o -name "*.doc" '
            '-o -name "*.pptx" -o -name "*.ppt" '
            '-o -name "*.xlsx" -o -name "*.xls" -o -name "*.csv" '
            '-o -name "*.html" -o -name "*.htm" '
            '-o -name "*.py" -o -name "*.js" -o -name "*.ts" -o -name "*.jsx" -o -name "*.tsx" '
            '-o -name "*.json" -o -name "*.yaml" -o -name "*.yml" '
            '-o -name "*.md" -o -name "*.txt" '
            '-o -name "*.png" -o -name "*.jpg" -o -name "*.jpeg" -o -name "*.gif" -o -name "*.svg" '
            '-o -name "*.zip" -o -name "*.tar.gz" '
            '-o -name "*.css" '
            '\\) '
            '2>/dev/null | head -50',
            timeout=30,
        )

        if not result.stdout or not result.stdout.strip():
            return {"sandbox_id": sandbox_id, "uploaded_files": [], "message": "No deliverable files found"}

        file_paths = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]

    if not file_paths:
        return {"sandbox_id": sandbox_id, "uploaded_files": [], "message": "No files to upload"}

    # Upload each file to Supabase Storage
    uploaded = []
    storage_prefix = req.session_id or sandbox_id

    async with httpx.AsyncClient(timeout=60.0) as client:
        for filepath in file_paths:
            try:
                # Read file as base64 from sandbox
                b64_result = sandbox.commands.run(
                    f'base64 -w 0 "{filepath}" 2>/dev/null',
                    timeout=30,
                )
                if not b64_result.stdout or b64_result.exit_code != 0:
                    continue

                file_bytes = base64.b64decode(b64_result.stdout.strip())
                name = filepath.split("/")[-1]
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                mime = _get_mime_type(ext)

                # Upload to Supabase Storage
                storage_path = f"{storage_prefix}/{name}"
                upload_url = f"{req.supabase_url}/storage/v1/object/{req.bucket}/{storage_path}"

                resp = await client.post(
                    upload_url,
                    headers={
                        "Authorization": f"Bearer {req.supabase_service_key}",
                        "apikey": req.supabase_service_key,
                        "Content-Type": mime,
                        "x-upsert": "true",
                    },
                    content=file_bytes,
                )

                if resp.status_code in (200, 201):
                    # Get public URL
                    public_url = f"{req.supabase_url}/storage/v1/object/public/{req.bucket}/{storage_path}"
                    uploaded.append({
                        "name": name,
                        "path": storage_path,
                        "extension": ext,
                        "mime_type": mime,
                        "size": len(file_bytes),
                        "url": public_url,
                        "sandbox_path": filepath,
                    })
                else:
                    uploaded.append({
                        "name": name,
                        "error": f"Upload failed: {resp.status_code} - {resp.text}",
                        "sandbox_path": filepath,
                    })

            except Exception as e:
                uploaded.append({
                    "name": filepath.split("/")[-1],
                    "error": str(e),
                    "sandbox_path": filepath,
                })

    return {
        "sandbox_id": sandbox_id,
        "uploaded_files": uploaded,
        "total_files": len(file_paths),
        "successful_uploads": len([f for f in uploaded if "url" in f]),
    }


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def _get_sandbox(sandbox_id: str) -> Sandbox:
    """Get or reconnect to a sandbox by ID."""
    if sandbox_id in active_sandboxes:
        return active_sandboxes[sandbox_id]
    try:
        sandbox = Sandbox.connect(sandbox_id=sandbox_id)
        active_sandboxes[sandbox_id] = sandbox
        return sandbox
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Sandbox not found or expired: {str(e)}")


def _get_mime_type(ext: str) -> str:
    """Map file extension to MIME type."""
    mime_map = {
        # Documents
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "ppt": "application/vnd.ms-powerpoint",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel",
        "csv": "text/csv",
        # Web
        "html": "text/html",
        "htm": "text/html",
        "css": "text/css",
        "js": "application/javascript",
        "ts": "application/typescript",
        "jsx": "application/javascript",
        "tsx": "application/typescript",
        "json": "application/json",
        # Text
        "md": "text/markdown",
        "txt": "text/plain",
        "yaml": "text/yaml",
        "yml": "text/yaml",
        "py": "text/x-python",
        # Images
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "webp": "image/webp",
        # Archives
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
