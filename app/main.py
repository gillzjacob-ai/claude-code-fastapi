import json
import os
from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
from e2b import Sandbox
from supabase import create_client
import base64

app = FastAPI()
from fastapi import Request, HTTPException
import os

# Supabase client for file uploads
supabase_url = os.getenv("SUPABASE_URL", "")
supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
supabase = create_client(supabase_url, supabase_key) if supabase_url and supabase_key else None

ARTIFACT_BUCKET = "agent-artifacts"
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB
OUTPUT_DIR = "/home/user/output"

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    token = request.headers.get("Authorization")
    expected = f"Bearer {os.getenv('API_AUTH_TOKEN', '')}"
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await call_next(request)

system_prompt = """
GitHub PAT is already set in the environment GITHUB_PAT. The repository is already cloned in the sandbox and the working directory is the repository root.

IMPORTANT: When you produce a substantial deliverable (report, document, plan, code, spreadsheet, website, etc.), also save it as a properly formatted file in /home/user/output/. Use appropriate file formats:
- Reports and plans: save as .md (markdown)
- Data and tables: save as .csv
- Websites and apps: save the .html, .css, and .js files
- Code projects: save all source files
- Presentations: save as .md with clear slide breaks
Create the /home/user/output/ directory first with: mkdir -p /home/user/output
Always announce any files you create by noting the filename at the end of your response.
"""

sandbox_template = os.getenv("E2B_SANDBOX_TEMPLATE", "claude-code-dev")
sandbox_timeout = 60 * 60  # 1 hour

# claude session id -> sandbox id
session_sandbox_map = {}


class ClaudePrompt(BaseModel):
    prompt: str
    repo: Optional[str] = None


def extract_and_upload_files(sandbox, session_id: str, agent_id: str = "default"):
    """Extract files from sandbox output directory and upload to Supabase Storage."""
    files = []

    if not supabase:
        return files

    try:
        # Check if output directory exists
        check = sandbox.commands.run(f"ls -la {OUTPUT_DIR} 2>/dev/null")
        if check.exit_code != 0:
            return files

        # List files in output directory
        file_list = sandbox.commands.run(
            f"find {OUTPUT_DIR} -type f -not -name '.*' 2>/dev/null"
        )
        if not file_list.stdout.strip():
            return files

        file_paths = file_list.stdout.strip().split("\n")

        for file_path in file_paths:
            file_path = file_path.strip()
            if not file_path:
                continue

            # Get file size
            size_check = sandbox.commands.run(f"stat -c%s '{file_path}' 2>/dev/null")
            if size_check.exit_code != 0:
                continue

            file_size = int(size_check.stdout.strip())
            if file_size > MAX_FILE_SIZE or file_size == 0:
                continue

            # Get filename
            filename = file_path.split("/")[-1]
            file_ext = filename.split(".")[-1].lower() if "." in filename else ""

            try:
                # Read file content from sandbox
                content = sandbox.files.read(file_path)

                # Upload to Supabase Storage
                storage_path = f"{session_id}/{agent_id}/{filename}"

                # Determine content type
                content_types = {
                    "pdf": "application/pdf",
                    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "csv": "text/csv",
                    "html": "text/html",
                    "css": "text/css",
                    "js": "application/javascript",
                    "json": "application/json",
                    "md": "text/markdown",
                    "txt": "text/plain",
                    "py": "text/x-python",
                    "png": "image/png",
                    "jpg": "image/jpeg",
                    "jpeg": "image/jpeg",
                    "svg": "image/svg+xml",
                    "zip": "application/zip",
                }
                content_type = content_types.get(file_ext, "application/octet-stream")

                # Upload to Supabase Storage
                # Content from sandbox.files.read() returns bytes
                file_content = content if isinstance(content, bytes) else content.encode("utf-8")

                supabase.storage.from_(ARTIFACT_BUCKET).upload(
                    path=storage_path,
                    file=file_content,
                    file_options={"content-type": content_type},
                )

                # Generate signed URL (24 hours)
                signed = supabase.storage.from_(ARTIFACT_BUCKET).create_signed_url(
                    path=storage_path,
                    expires_in=86400,  # 24 hours
                )

                files.append({
                    "filename": filename,
                    "file_type": file_ext,
                    "size_bytes": file_size,
                    "download_url": signed["signedURL"] if isinstance(signed, dict) else signed.signed_url,
                    "storage_path": storage_path,
                })

            except Exception as e:
                print(f"Error processing file {file_path}: {e}")
                continue

    except Exception as e:
        print(f"Error extracting files: {e}")

    return files


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

    # Create output directory in sandbox
    sandbox.commands.run(f"mkdir -p {OUTPUT_DIR}")

    cmd = "claude"
    claude_args = [
        "-p",
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
        "--mcp-config",
        "/.mcp/mcp.json",
        "--append-system-prompt",
        f'"{system_prompt}"',
    ]

    if session:
        claude_args.append(f"--resume")
        claude_args.append(session)

    response = sandbox.commands.run(
        f"echo '{prompt.prompt}' | {cmd} {' '.join(claude_args)}",
        timeout=0,
    )

    if response.stderr:
        raise HTTPException(status_code=500, detail=response.stderr)

    claude_response = json.loads(response.stdout)
    session_sandbox_map[claude_response["session_id"]] = sandbox.sandbox_id

    # Extract files from sandbox and upload to Supabase Storage
    agent_id = claude_response.get("session_id", "default")
    files = extract_and_upload_files(sandbox, claude_response["session_id"], agent_id)

    # Add files to the response
    claude_response["files"] = files

    return claude_response
