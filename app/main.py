import json
import os
from typing import Optional
from pydantic import BaseModel

from fastapi import FastAPI, HTTPException
from e2b import Sandbox

app = FastAPI()

sandbox_template = os.getenv("E2B_SANDBOX_TEMPLATE", "claude-code-dev")
sandbox_timeout = 60 * 60

# claude session id -> sandbox id
session_sandbox_map = {}


class ClaudeSession(BaseModel):
    prompt: str
    resume: Optional[str] = None
    repo: Optional[str] = None


@app.post("/chat")
def prompt(session: ClaudeSession):
    if session.resume is None:
        sandbox = Sandbox(
            template=sandbox_template,
            timeout=sandbox_timeout,
            envs={"GITHUB_PAT": os.getenv("GITHUB_PAT", "")},
        )
        if session.repo:
            sandbox.commands.run(
                f"git clone {session.repo} && cd {session.repo.split('/')[-1]}"
            )
    else:
        sandbox = Sandbox(sandbox_id=session_sandbox_map[session.resume])

    cmd = "claude"
    claude_args = [
        "-p",
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
        "--mcp-config",
        "/.mcp/mcp.json",
    ]
    if session.resume:
        claude_args.append(f"--resume")
        claude_args.append(session.resume)

    response = sandbox.commands.run(
        f"echo '{session.prompt}' | {cmd} {' '.join(claude_args)}",
        envs={"ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", "")},
        timeout=0,
    )

    if response.stderr:
        raise HTTPException(status_code=500, detail=response.stderr)

    claude_response = json.loads(response.stdout)
    session_sandbox_map[claude_response["session_id"]] = sandbox.sandbox_id

    return claude_response
