import json
import os
import re
import shlex
import uuid
import threading
from typing import Optional
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from e2b import Sandbox
from supabase import create_client, Client

# ------------------------------------------
# Startup Validation
# ------------------------------------------
REQUIRED_ENV_VARS = ["ANTHROPIC_API_KEY", "E2B_API_KEY", "API_AUTH_TOKEN"]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(missing)}. "
        "Set these in your Railway environment before deploying."
    )

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY. "
        "Set these in your Railway environment to enable persistent job tracking."
    )

# ------------------------------------------
# Supabase Client
# ------------------------------------------
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()


# ------------------------------------------
# Auth middleware
# ------------------------------------------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    token = request.headers.get("Authorization")
    expected = f"Bearer {os.getenv('API_AUTH_TOKEN', '')}"
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await call_next(request)


# ------------------------------------------
# Config
# ------------------------------------------
system_prompt = """
GitHub PAT is already set in the environment GITHUB_PAT. The repository is already cloned in the sandbox and the working directory is the repository root.

You are an autonomous agent in the Clustor platform. Your outputs are displayed directly to users in a professional interface. Follow these output rules:

OUTPUT FORMATTING:
- Structure your response with clear markdown sections using ## headers
- For email tasks: list each email as a separate item with **From**, **Subject**, **Priority** (High/Medium/Low), and a one-line preview. Group by priority.
- For monitoring/analytics tasks: lead with key metrics as a summary dashboard, then provide detailed breakdown sections.
- For research tasks: start with a Key Findings section (3-5 bullet points), then detailed sections with sources.
- For content creation: deliver the content directly as a polished artifact.
- For multi-step tasks: show a brief status for each step completed.
- Always be concise but thorough. No filler text. Every sentence should add value.
- Never dump raw data. Always interpret and organize it for a busy professional.
"""

sandbox_template = os.getenv("E2B_SANDBOX_TEMPLATE", "claude-code-dev")
sandbox_timeout = 60 * 60  # 1 hour

# Local cache of sandbox objects (runtime only, source of truth is Supabase)
active_sandboxes = {}


# ------------------------------------------
# Models
# ------------------------------------------
class ClaudePrompt(BaseModel):
    prompt: str
    repo: Optional[str] = None
    composio_mcp_url: Optional[str] = None
    composio_api_key: Optional[str] = None


class FileInfo(BaseModel):
    path: str
    name: str
    size: int
    extension: str


class ScheduleCreate(BaseModel):
    name: str
    agent_prompt: str
    cron_expression: str
    enabled: bool = True
    sandbox_template: Optional[str] = None
    composio_entity_id: Optional[str] = None
    composio_api_key: Optional[str] = None
    composio_mcp_url: Optional[str] = None


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    agent_prompt: Optional[str] = None
    cron_expression: Optional[str] = None
    enabled: Optional[bool] = None


# ------------------------------------------
# Supabase Helpers - Jobs
# ------------------------------------------
def create_job(job_id: str, schedule_id: Optional[str] = None):
    """Insert a new job record into Supabase."""
    db.table("agent_jobs").insert({
        "job_id": job_id,
        "status": "processing",
        "sandbox_id": None,
        "session_id": None,
        "result": None,
        "error": None,
        "schedule_id": schedule_id,
    }).execute()


def update_job(job_id: str, **fields):
    """Update a job record in Supabase."""
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    db.table("agent_jobs").update(fields).eq("job_id", job_id).execute()


def get_job(job_id: str):
    """Fetch a job record from Supabase."""
    result = db.table("agent_jobs").select("*").eq("job_id", job_id).execute()
    if result.data and len(result.data) > 0:
        return result.data[0]
    return None


def save_session_sandbox(session_id: str, sandbox_id: str):
    """Persist the session-to-sandbox mapping in Supabase."""
    db.table("session_sandboxes").upsert({
        "session_id": session_id,
        "sandbox_id": sandbox_id,
    }).execute()


def get_sandbox_for_session(session_id: str) -> Optional[str]:
    """Look up the sandbox ID for a given session."""
    result = (
        db.table("session_sandboxes")
        .select("sandbox_id")
        .eq("session_id", session_id)
        .execute()
    )
    if result.data and len(result.data) > 0:
        return result.data[0]["sandbox_id"]
    return None


# ------------------------------------------
# Supabase Helpers - Schedules & Agent State
# ------------------------------------------
def get_all_enabled_schedules():
    """Fetch all enabled schedules from Supabase."""
    result = db.table("schedules").select("*").eq("enabled", True).execute()
    return result.data or []


def get_schedule(schedule_id: str):
    """Fetch a single schedule by ID."""
    result = db.table("schedules").select("*").eq("id", schedule_id).execute()
    if result.data and len(result.data) > 0:
        return result.data[0]
    return None


def update_schedule(schedule_id: str, **fields):
    """Update a schedule record."""
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    db.table("schedules").update(fields).eq("id", schedule_id).execute()


def get_agent_state(schedule_id: str) -> dict:
    """Load the persisted state for a scheduled agent."""
    result = db.table("schedules").select("last_state").eq("id", schedule_id).execute()
    if result.data and len(result.data) > 0:
        return result.data[0].get("last_state") or {}
    return {}


def save_agent_state(schedule_id: str, state: dict):
    """Persist the agent's state after a scheduled run."""
    update_schedule(
        schedule_id,
        last_state=state,
        last_run_at=datetime.now(timezone.utc).isoformat(),
    )


def record_agent_run(schedule_id: str, job_id: str, status: str, summary: Optional[str] = None, error: Optional[str] = None, result_type: Optional[str] = None):
    """Insert a row into agent_runs for full run history."""
    db.table("agent_runs").insert({
        "id": str(uuid.uuid4()),
        "schedule_id": schedule_id,
        "job_id": job_id,
        "status": status,
        "summary": summary,
        "error": error,
        "result_type": result_type,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


# ------------------------------------------
# Composio MCP Session Refresh
# ------------------------------------------
def refresh_composio_mcp_url(entity_id: str, api_key: str) -> Optional[str]:
    """
    Create a fresh Composio session for the user and return the MCP URL.
    Called before each scheduled run to ensure the MCP URL hasn't expired.
    """
    try:
        resp = httpx.post(
            "https://backend.composio.dev/api/v3/tool_router/session",
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={"user_id": entity_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        url = data.get("mcp", {}).get("url", "")
        if url:
            print(f"[Composio] Refreshed MCP URL for entity {entity_id}")
            return url
        return None
    except Exception as e:
        print(f"[Composio] MCP refresh failed for entity {entity_id}: {e}")
        return None


# ------------------------------------------
# Core Agent Runner (shared by /chat and scheduler)
# ------------------------------------------
def run_agent_in_background(
    job_id: str,
    prompt_text: str,
    repo: Optional[str],
    session: Optional[str],
    schedule_id: Optional[str] = None,
    composio_mcp_url: Optional[str] = None,
    composio_api_key: Optional[str] = None,
):
    try:
        # Build sandbox environment variables
        sandbox_envs = {
            "GITHUB_PAT": os.getenv("GITHUB_PAT", ""),
            "CONTEXT7_API_KEY": os.getenv("CONTEXT7_API_KEY", ""),
            "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
        }

        # Inject Composio credentials if available
        if composio_mcp_url:
            sandbox_envs["COMPOSIO_MCP_URL"] = composio_mcp_url
        if composio_api_key:
            sandbox_envs["COMPOSIO_API_KEY"] = composio_api_key

        if session is None:
            sandbox = Sandbox.create(
                template=sandbox_template,
                timeout=sandbox_timeout,
                envs=sandbox_envs,
            )
            if repo:
                sandbox.commands.run(
                    f"git clone {repo} && cd {repo.split('/')[-1]}"
                )
        else:
            sandbox_id = get_sandbox_for_session(session)
            if not sandbox_id:
                update_job(job_id, status="error", error=f"No sandbox found for session {session}")
                if schedule_id:
                    record_agent_run(schedule_id, job_id, "error", error=f"No sandbox for session {session}")
                return
            sandbox = Sandbox.connect(sandbox_id=sandbox_id)

        # Store sandbox reference in local cache
        active_sandboxes[sandbox.sandbox_id] = sandbox
        update_job(job_id, sandbox_id=sandbox.sandbox_id)

        # Write .mcp.json into the sandbox at runtime with current Composio URL
        # This overrides the baked-in template config so we don't need to rebuild the E2B image
        mcp_config = {
            "mcpServers": {
                "context7": {
                    "type": "http",
                    "url": "https://mcp.context7.com/mcp",
                    "headers": {
                        "Authorization": f"Bearer {sandbox_envs.get('CONTEXT7_API_KEY', '')}"
                    }
                }
            }
        }
        if composio_mcp_url:
            mcp_config["mcpServers"]["composio"] = {
                "type": "http",
                "url": composio_mcp_url,
                "headers": {
                    "Authorization": f"Bearer {composio_api_key or ''}"
                }
            }
        mcp_json_str = json.dumps(mcp_config, indent=2)
        # Write to home directory where Claude Code looks for .mcp.json
        sandbox.commands.run(
            f"echo {shlex.quote(mcp_json_str)} > /home/user/.mcp.json",
            timeout=10,
        )
        # Also try the working directory
        sandbox.commands.run(
            f"echo {shlex.quote(mcp_json_str)} > .mcp.json",
            timeout=10,
        )

        # Build Claude CLI command
        cmd = "claude"
        claude_args = [
            "-p",
            "--dangerously-skip-permissions",
            "--output-format", "json",
            "--model", "claude-sonnet-4-6",
            "--append-system-prompt", shlex.quote(system_prompt),
        ]

        if session:
            claude_args.append("--resume")
            claude_args.append(session)

        # Write prompt to temp file to avoid shell injection
        safe_prompt = json.dumps(prompt_text)
        sandbox.commands.run(
            f"echo {shlex.quote(safe_prompt)} > /tmp/agent_prompt.txt",
            timeout=30,
        )

        response = sandbox.commands.run(
            f"cat /tmp/agent_prompt.txt | {cmd} {' '.join(claude_args)}",
            timeout=0,
        )

        if response.stderr:
            update_job(job_id, status="error", error=response.stderr)
            if schedule_id:
                record_agent_run(schedule_id, job_id, "error", error=response.stderr)
            return

        claude_response = json.loads(response.stdout)

        # Persist session-to-sandbox mapping
        if "session_id" in claude_response:
            save_session_sandbox(claude_response["session_id"], sandbox.sandbox_id)

        claude_response["sandbox_id"] = sandbox.sandbox_id

        update_job(
            job_id,
            status="complete",
            result=claude_response,
            session_id=claude_response.get("session_id"),
        )

        # Scheduled agent: extract & persist state
        if schedule_id:
            result_text = claude_response.get("result", "") or ""
            new_state = _extract_agent_state(result_text)
            if new_state:
                save_agent_state(schedule_id, new_state)

            # Extract result type for frontend rendering
            result_type = _extract_result_type(result_text)

            record_agent_run(
                schedule_id,
                job_id,
                "complete",
                summary=result_text[:2000] if result_text else None,
                result_type=result_type,
            )

    except Exception as e:
        update_job(job_id, status="error", error=str(e))
        if schedule_id:
            record_agent_run(schedule_id, job_id, "error", error=str(e))


def _extract_agent_state(result_text: str) -> Optional[dict]:
    """
    Extract the __agent_state__ JSON block from Claude's response.
    """
    pattern = r'```json\s*(\{.*?"__agent_state__".*?\})\s*```'
    match = re.search(pattern, result_text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            return parsed.get("__agent_state__")
        except json.JSONDecodeError:
            pass
    return None


def _extract_result_type(result_text: str) -> str:
    """
    Extract the __result_type__ hint from Claude's response.
    Used by the frontend to choose the right rendering component.
    """
    match = re.search(r'__result_type__:\s*(\w+)', result_text)
    if match:
        return match.group(1)
    return "task_update"


# ------------------------------------------
# Scheduler - runs scheduled agents on cron
# ------------------------------------------
scheduler = BackgroundScheduler(timezone="UTC")


def _build_scheduled_prompt(schedule: dict) -> str:
    """
    Build the full prompt for a scheduled agent run.
    Includes: base task, previous state, output formatting, and state persistence instructions.
    """
    base_prompt = schedule["agent_prompt"]
    last_state = schedule.get("last_state") or {}

    state_block = ""
    if last_state:
        state_block = f"""

--- PREVIOUS RUN STATE ---
Below is what you already processed. Skip these items entirely and only report NEW items since your last run.
{json.dumps(last_state, indent=2)}
--- END PREVIOUS STATE ---
"""

    return f"""You are an autonomous agent running a scheduled task on the Clustor platform. Your output is displayed directly in a professional dashboard UI that renders markdown beautifully.

TASK: {base_prompt}
{state_block}
OUTPUT RULES:
1. First line must be: __result_type__: [email_summary | performance_report | research_brief | content_delivery | task_update | alert] (pick the best fit)
2. Second line: STATUS: [one sentence summary of what you found]
3. Then provide your FULL DETAILED results using well-structured markdown. Use headers, bold, lists, and tables as appropriate for the content. Be thorough — list every individual item, don't summarize multiple items into one sentence. Your output should look like something a professional analyst would deliver, not a quick text message.
4. End with your state update so the next scheduled run knows what was already handled:
```json
{{"__agent_state__": {{
  "last_processed_ids": ["IDs of items you processed"],
  "last_checked_at": "{datetime.now(timezone.utc).isoformat()}",
  "summary": "brief note of what was found",
  "items_found": 0
}}}}
```"""


def _run_scheduled_agent(schedule_id: str):
    """
    Fired by APScheduler on each cron tick.
    Loads the schedule from Supabase, refreshes Composio MCP if available,
    builds the prompt, and runs Claude with full tool access.
    """
    schedule = get_schedule(schedule_id)
    if not schedule or not schedule.get("enabled"):
        return  # Schedule was disabled between ticks - skip

    job_id = str(uuid.uuid4())
    create_job(job_id, schedule_id=schedule_id)

    prompt_text = _build_scheduled_prompt(schedule)

    # Resolve Composio MCP URL - refresh if we have entity credentials
    composio_mcp_url = schedule.get("composio_mcp_url")
    composio_api_key = schedule.get("composio_api_key")
    composio_entity_id = schedule.get("composio_entity_id")

    if composio_entity_id and composio_api_key:
        fresh_url = refresh_composio_mcp_url(composio_entity_id, composio_api_key)
        if fresh_url:
            composio_mcp_url = fresh_url
            # Update stored URL so it's fresh for next time
            update_schedule(schedule_id, composio_mcp_url=fresh_url)

    thread = threading.Thread(
        target=run_agent_in_background,
        args=(job_id, prompt_text, None, None, schedule_id),
        kwargs={
            "composio_mcp_url": composio_mcp_url,
            "composio_api_key": composio_api_key,
        },
        daemon=True,
    )
    thread.start()


def _load_schedules_into_scheduler():
    """
    On startup, load all enabled schedules from Supabase and register them
    with APScheduler. Called once at app startup.
    """
    schedules = get_all_enabled_schedules()
    for schedule in schedules:
        _register_schedule(schedule)
    print(f"[Scheduler] Loaded {len(schedules)} schedule(s) from Supabase.")


def _register_schedule(schedule: dict):
    """Add or replace a schedule in APScheduler."""
    job_id = f"schedule_{schedule['id']}"
    # Remove existing job if it's already registered (for updates)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if schedule.get("enabled"):
        scheduler.add_job(
            _run_scheduled_agent,
            trigger=CronTrigger.from_crontab(schedule["cron_expression"], timezone="UTC"),
            id=job_id,
            args=[schedule["id"]],
            replace_existing=True,
            misfire_grace_time=300,  # 5 min grace window if server was down
        )


def _unregister_schedule(schedule_id: str):
    """Remove a schedule from APScheduler."""
    job_id = f"schedule_{schedule_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


# ------------------------------------------
# App Lifecycle - start/stop scheduler
# ------------------------------------------
@app.on_event("startup")
def startup_event():
    _load_schedules_into_scheduler()
    scheduler.start()
    print("[Scheduler] Started.")


@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown(wait=False)
    print("[Scheduler] Stopped.")


# ------------------------------------------
# POST /chat - returns immediately with job_id
# ------------------------------------------
@app.post("/chat/{session}")
@app.post("/chat")
def prompt(prompt: ClaudePrompt, session: Optional[str] = None):
    job_id = str(uuid.uuid4())
    create_job(job_id)

    thread = threading.Thread(
        target=run_agent_in_background,
        args=(job_id, prompt.prompt, prompt.repo, session),
        kwargs={
            "composio_mcp_url": prompt.composio_mcp_url,
            "composio_api_key": prompt.composio_api_key,
        },
        daemon=True,
    )
    thread.start()

    return {
        "job_id": job_id,
        "status": "processing",
        "message": "Agent started. Poll GET /result/{job_id} for status.",
    }


# ------------------------------------------
# GET /result/{job_id} - Poll for agent completion
# ------------------------------------------
@app.get("/result/{job_id}")
def get_result(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "sandbox_id": job["sandbox_id"],
        "result": job["result"],
        "error": job["error"],
        "session_id": job["session_id"],
        "schedule_id": job.get("schedule_id"),
    }


# ------------------------------------------
# POST /schedules - Create a new scheduled agent
# ------------------------------------------
@app.post("/schedules")
def create_schedule(body: ScheduleCreate):
    result = db.table("schedules").insert({
        "name": body.name,
        "agent_prompt": body.agent_prompt,
        "cron_expression": body.cron_expression,
        "enabled": body.enabled,
        "sandbox_template": body.sandbox_template,
        "composio_entity_id": body.composio_entity_id,
        "composio_api_key": body.composio_api_key,
        "composio_mcp_url": body.composio_mcp_url,
        "last_state": None,
        "last_run_at": None,
    }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create schedule")

    schedule = result.data[0]
    _register_schedule(schedule)

    return {
        "id": schedule["id"],
        "name": schedule["name"],
        "cron_expression": schedule["cron_expression"],
        "enabled": schedule["enabled"],
        "message": f"Schedule created and registered. Next run: {_next_run_time(schedule['id'])}",
    }


# ------------------------------------------
# GET /schedules - List all schedules
# ------------------------------------------
@app.get("/schedules")
def list_schedules():
    result = db.table("schedules").select("*").order("created_at", desc=True).execute()
    schedules = result.data or []
    for s in schedules:
        s["next_run_at"] = _next_run_time(s["id"])
    return {"schedules": schedules}


# ------------------------------------------
# GET /schedules/{schedule_id} - Single schedule + recent runs
# ------------------------------------------
@app.get("/schedules/{schedule_id}")
def get_schedule_detail(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    runs = (
        db.table("agent_runs")
        .select("*")
        .eq("schedule_id", schedule_id)
        .order("ran_at", desc=True)
        .limit(10)
        .execute()
    )

    schedule["next_run_at"] = _next_run_time(schedule_id)
    schedule["recent_runs"] = runs.data or []
    return schedule


# ------------------------------------------
# PATCH /schedules/{schedule_id} - Update a schedule
# ------------------------------------------
@app.patch("/schedules/{schedule_id}")
def patch_schedule(schedule_id: str, body: ScheduleUpdate):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    update_schedule(schedule_id, **updates)

    updated = get_schedule(schedule_id)
    if updated["enabled"]:
        _register_schedule(updated)
    else:
        _unregister_schedule(schedule_id)

    return {"id": schedule_id, "updated": updates, "next_run_at": _next_run_time(schedule_id)}


# ------------------------------------------
# DELETE /schedules/{schedule_id} - Delete a schedule
# ------------------------------------------
@app.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    _unregister_schedule(schedule_id)
    db.table("schedules").delete().eq("id", schedule_id).execute()
    return {"id": schedule_id, "deleted": True}


# ------------------------------------------
# POST /schedules/{schedule_id}/run - Trigger manually right now
# ------------------------------------------
@app.post("/schedules/{schedule_id}/run")
def trigger_schedule_now(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    job_id = str(uuid.uuid4())
    create_job(job_id, schedule_id=schedule_id)

    prompt_text = _build_scheduled_prompt(schedule)

    # Refresh Composio MCP URL if credentials available
    composio_mcp_url = schedule.get("composio_mcp_url")
    composio_api_key = schedule.get("composio_api_key")
    composio_entity_id = schedule.get("composio_entity_id")

    if composio_entity_id and composio_api_key:
        fresh_url = refresh_composio_mcp_url(composio_entity_id, composio_api_key)
        if fresh_url:
            composio_mcp_url = fresh_url

    thread = threading.Thread(
        target=run_agent_in_background,
        args=(job_id, prompt_text, None, None, schedule_id),
        kwargs={
            "composio_mcp_url": composio_mcp_url,
            "composio_api_key": composio_api_key,
        },
        daemon=True,
    )
    thread.start()

    return {
        "job_id": job_id,
        "schedule_id": schedule_id,
        "status": "processing",
        "message": "Manual trigger fired. Poll GET /result/{job_id} for status.",
    }


# ------------------------------------------
# GET /schedules/{schedule_id}/state - View current agent state
# ------------------------------------------
@app.get("/schedules/{schedule_id}/state")
def get_schedule_state(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {
        "schedule_id": schedule_id,
        "last_state": schedule.get("last_state") or {},
        "last_run_at": schedule.get("last_run_at"),
    }


# ------------------------------------------
# DELETE /schedules/{schedule_id}/state - Reset agent state
# ------------------------------------------
@app.delete("/schedules/{schedule_id}/state")
def reset_schedule_state(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    update_schedule(schedule_id, last_state=None)
    return {"schedule_id": schedule_id, "last_state": None, "message": "State cleared. Next run will start fresh."}


# ------------------------------------------
# GET /files/{sandbox_id} - List files in sandbox
# ------------------------------------------
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


# ------------------------------------------
# GET /files/{sandbox_id}/download - Single file as base64
# ------------------------------------------
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


# ------------------------------------------
# Helpers
# ------------------------------------------
def _get_sandbox(sandbox_id: str) -> Sandbox:
    if sandbox_id in active_sandboxes:
        return active_sandboxes[sandbox_id]
    try:
        sandbox = Sandbox.connect(sandbox_id=sandbox_id)
        active_sandboxes[sandbox_id] = sandbox
        return sandbox
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Sandbox not found or expired: {str(e)}")


def _next_run_time(schedule_id: str) -> Optional[str]:
    """Return the next scheduled run time as an ISO string, or None."""
    job = scheduler.get_job(f"schedule_{schedule_id}")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


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


# ------------------------------------------
# Health check
# ------------------------------------------
@app.get("/health")
def health():
    scheduled_jobs = len(scheduler.get_jobs())
    return {"status": "ok", "scheduled_jobs": scheduled_jobs}
