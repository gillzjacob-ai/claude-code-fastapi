import json
import os
from typing import Optional
from pydantic import BaseModel

from fastapi import FastAPI, HTTPException
from e2b import Sandbox

app = FastAPI()
from fastapi import Request, HTTPException
import os

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
"""

sandbox_template = os.getenv("E2B_SANDBOX_TEMPLATE", "claude-code-dev")
sandbox_timeout = 60 * 60 # 1 hour

# claude session id -> sandbox id
session_sandbox_map = {}


class ClaudePrompt(BaseModel):
    prompt: str
    repo: Optional[str] = None


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
        claude_args.append(f"--resume")
        claude_args.append(session)

   response = sandbox.commands.run(
        f"echo {json.dumps(prompt.prompt)} | {cmd} {' '.join(claude_args)}",
        timeout=0,
    )

    if response.stderr:
        raise HTTPException(status_code=500, detail=response.stderr)

    claude_response = json.loads(response.stdout)
    session_sandbox_map[claude_response["session_id"]] = sandbox.sandbox_id

    return claude_response
