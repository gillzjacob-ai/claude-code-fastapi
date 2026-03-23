import json
import os
import base64
from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
from e2b import Sandbox

app = FastAPI()
from fastapi import Request, HTTPException
import os

OUTPUT_DIR = "/home/user/output"
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    token = request.headers.get("Authorization")
    expected = f"Bearer {os.getenv('API_AUTH_TOKEN', '')}"
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await call_next(request)

SYSTEM_PROMPT = (
    "GitHub PAT is already set in the environment GITHUB_PAT. "
    "The repository is already cloned in the sandbox and the working directory is the repository root.\n\n"
    "IMPORTANT: When you produce a substantial deliverable (report, document, plan, code, spreadsheet, website, etc.), "
    "also save it as a properly formatted file in /home/user/output/. Use appropriate file formats:\n"
    "- Reports and plans: save as .md (markdown)\n"
    "- Data and tables: save as .csv\n"
    "- Websites and apps: save the .html, .css, and .js files\n"
    "- Code projects: save all source files\n"
    "- Presentations: save as .md with clear slide breaks\n"
    "Create the /home/user/output/ directory first with: mkdir -p /home/user/output\n"
    "Always announce any files you create by noting the filename at the end of your response."
)

sandbox_template = os.getenv("E2B_SANDBOX_TEMPLATE", "claude-code-dev")
sandbox_timeout = 60 * 60  # 1 hour

# claude session id -> sandbox id
session_sandbox_map = {}


class ClaudePrompt(BaseModel):
    prompt: str
    repo: Optional[str] = None


def extract_files_from_sandbox(sandbox):
    """Extract files from sandbox output directory and return as base64."""
    files = []

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

                # Convert to base64
                file_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
                file_base64 = base64.b64encode(file_bytes).decode("utf-8")

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

                files.append({
                    "filename": filename,
                    "file_type": file_ext,
                    "content_type": content_type,
                    "size_bytes": file_size,
                    "data": file_base64,
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

    # Write prompt and system prompt to files inside the sandbox
    # This avoids all shell escaping issues with special characters
    sandbox.files.write("/tmp/prompt.txt", prompt.prompt)
    sandbox.files.write("/tmp/system_prompt.txt", SYSTEM_PROMPT)

    cmd = "claude"
    claude_args = [
        "-p",
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
        "--mcp-config",
        "/.mcp/mcp.json",
    ]

    if session:
        claude_args.append("--resume")
        claude_args.append(session)

    # Use cat to pipe prompt and read system prompt from files
    # This safely handles any special characters in the prompt text
    response = sandbox.commands.run(
        f"cat /tmp/prompt.txt | {cmd} {' '.join(claude_args)} --append-system-prompt \"$(cat /tmp/system_prompt.txt)\"",
        timeout=0,
    )

    if response.stderr:
        raise HTTPException(status_code=500, detail=response.stderr)

    claude_response = json.loads(response.stdout)
    session_sandbox_map[claude_response["session_id"]] = sandbox.sandbox_id

    # Extract files from sandbox as base64
    files = extract_files_from_sandbox(sandbox)

    # Add files to the response
    claude_response["files"] = files

    return claude_response
