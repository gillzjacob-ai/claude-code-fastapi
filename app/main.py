import json
import os
import shlex
import uuid
import threading
from typing import Optional
from datetime import datetime, timezone

from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from e2b import Sandbox
from supabase import create_client, Client

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Startup Validation
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Supabase Client
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Auth middleware
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    token = request.headers.get("Authorization")
    expected = f"Bearer {os.getenv('API_AUTH_TOKEN', '')}"
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await call_next(request)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Config
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
system_prompt = """
GitHub PAT is already set in the environment GITHUB_PAT. The repository is already cloned in the sandbox and the working directory is the repository root.
"""

sandbox_template = os.getenv("E2B_SANDBOX_TEMPLATE", "claude-code-dev")
sandbox_timeout = 60 * 60  # 1 hour

# Local cache of sandbox objects (still needed for active connections)
# This is a runtime cache only â€” the source of truth is Supabase
active_sandboxes = {}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Models
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class ClaudePrompt(BaseModel):
    prompt: str
    repo: Optional[str] = None


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


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    agent_prompt: Optional[str] = None
    cron_expression: Optional[str] = None
    enabled: Optional[bool] = None


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Supabase Helpers â€” Jobs
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Supabase Helpers â€” Schedules & Agent State
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    """
    Load the persisted state for a scheduled agent.
    Returns the last_state JSONB from the schedules table.
    This is what lets 'check email every 4h' skip already-processed items.
    """
    result = db.table("schedules").select("last_state").eq("id", schedule_id).execute()
    if result.data and len(result.data) > 0:
        return result.data[0].get("last_state") or {}
    return {}


def save_agent_state(schedule_id: str, state: dict):
    """
    Persist the agent's state after a scheduled run.
    Stored as JSONB in schedules.last_state.
    Claude is instructed to return a JSON block with updated state
    (e.g. last processed email IDs, last checked ad metrics timestamp, etc.)
    and we extract + save it here.
    """
    update_schedule(
        schedule_id,
        last_state=state,
        last_run_at=datetime.now(timezone.utc).isoformat(),
    )


def record_agent_run(schedule_id: str, job_id: str, status: str, summary: Optional[str] = None, error: Optional[str] = None):
    """
    Insert a row into agent_runs for full run history.
    This is the audit trail â€” schedules.last_state is just the live snapshot.
    """
    db.table("agent_runs").insert({
        "id": str(uuid.uuid4()),
        "schedule_id": schedule_id,
        "job_id": job_id,
        "status": status,
        "summary": summary,
        "error": error,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Core Agent Runner (shared by /chat and scheduler)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def run_agent_in_background(
    job_id: str,
    prompt_text: str,
    repo: Optional[str],
    session: Optional[str],
    schedule_id: Optional[str] = None,
):
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

        # â”€â”€ Scheduled agent: extract & persist state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Claude is prompted to return a JSON block like:
        #   {"__agent_state__": {"last_email_ids": [...], "last_checked_at": "..."}}
        # We extract it here and store it for the next run.
        if schedule_id:
            result_text = claude_response.get("result", "") or ""
            new_state = _extract_agent_state(result_text)
            if new_state:
                save_agent_state(schedule_id, new_state)

            record_agent_run(
                schedule_id,
                job_id,
                "complete",
                summary=result_text[:500] if result_text else None,
            )

    except Exception as e:
        update_job(job_id, status="error", error=str(e))
        if schedule_id:
            record_agent_run(schedule_id, job_id, "error", error=str(e))


def _extract_agent_state(result_text: str) -> Optional[dict]:
    """
    Extract the __agent_state__ JSON block from Claude's response.
    Claude is instructed in the scheduled prompt to always end its response with:
      ```json
      {"__agent_state__": { ... }}
      ```
    We parse this out and store it so the next run knows what was already handled.
    """
    import re
    # Look for a JSON block containing __agent_state__
    pattern = r'```json\s*(\{.*?"__agent_state__".*?\})\s*```'
    match = re.search(pattern, result_text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            return parsed.get("__agent_state__")
        except json.JSONDecodeError:
            pass
    return None


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Scheduler â€” runs scheduled agents on cron
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
scheduler = BackgroundScheduler(timezone="UTC")


def _build_scheduled_prompt(schedule: dict) -> str:
    """
    Inject the agent's last known state into the prompt so it knows
    what was already processed and won't repeat itself.
    """
    base_prompt = schedule["agent_prompt"]
    last_state = schedule.get("last_state") or {}

    state_block = ""
    if last_state:
        state_block = f"""

--- AGENT STATE FROM LAST RUN ---
This is what you already processed in your previous run. Do NOT re-report or re-act on anything listed here.
{json.dumps(last_state, indent=2)}
--- END AGENT STATE ---
"""

    return f"""{base_prompt}{state_block}

IMPORTANT: At the end of your response, always include a JSON block with your updated state so the next run knows what you handled. Format it exactly like this:
```json
{{"__agent_state__": {{
  "last_processed_ids": ["list any IDs, email IDs, ad IDs, etc. you handled"],
  "last_checked_at": "{datetime.now(timezone.utc).isoformat()}",
  "notes": "any other state you want to remember for next run"
}}}}
```"""


def _run_scheduled_agent(schedule_id: str):
    """
    Fired by APScheduler on each cron tick.
    Loads the schedule + last state from Supabase, builds the prompt, runs Claude.
    """
    schedule = get_schedule(schedule_id)
    if not schedule or not schedule.get("enabled"):
        return  # Schedule was disabled between ticks â€” skip

    job_id = str(uuid.uuid4())
    create_job(job_id, schedule_id=schedule_id)

    prompt_text = _build_scheduled_prompt(schedule)
    tmpl = schedule.get("sandbox_template") or sandbox_template

    thread = threading.Thread(
        target=run_agent_in_background,
        args=(job_id, prompt_text, None, None, schedule_id),
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# App Lifecycle â€” start/stop scheduler
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.on_event("startup")
def startup_event():
    _load_schedules_into_scheduler()
    scheduler.start()
    print("[Scheduler] Started.")


@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown(wait=False)
    print("[Scheduler] Stopped.")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# POST /chat â€” ASYNC: returns immediately with job_id
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.post("/chat/{session}")
@app.post("/chat")
def prompt(prompt: ClaudePrompt, session: Optional[str] = None):
    job_id = str(uuid.uuid4())
    create_job(job_id)

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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /result/{job_id} â€” Poll for agent completion
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# POST /schedules â€” Create a new scheduled agent
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.post("/schedules")
def create_schedule(body: ScheduleCreate):
    result = db.table("schedules").insert({
        "name": body.name,
        "agent_prompt": body.agent_prompt,
        "cron_expression": body.cron_expression,
        "enabled": body.enabled,
        "sandbox_template": body.sandbox_template,
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /schedules â€” List all schedules
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.get("/schedules")
def list_schedules():
    result = db.table("schedules").select("*").order("created_at", desc=True).execute()
    schedules = result.data or []
    # Attach next run time from APScheduler
    for s in schedules:
        s["next_run_at"] = _next_run_time(s["id"])
    return {"schedules": schedules}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /schedules/{schedule_id} â€” Get a single schedule + recent runs
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.get("/schedules/{schedule_id}")
def get_schedule_detail(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Fetch last 10 runs
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# PATCH /schedules/{schedule_id} â€” Update a schedule
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.patch("/schedules/{schedule_id}")
def patch_schedule(schedule_id: str, body: ScheduleUpdate):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    update_schedule(schedule_id, **updates)

    # Re-register with APScheduler to pick up any cron/enabled changes
    updated = get_schedule(schedule_id)
    if updated["enabled"]:
        _register_schedule(updated)
    else:
        _unregister_schedule(schedule_id)

    return {"id": schedule_id, "updated": updates, "next_run_at": _next_run_time(schedule_id)}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DELETE /schedules/{schedule_id} â€” Delete a schedule
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    _unregister_schedule(schedule_id)
    db.table("schedules").delete().eq("id", schedule_id).execute()
    return {"id": schedule_id, "deleted": True}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# POST /schedules/{schedule_id}/run â€” Trigger a schedule manually right now
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.post("/schedules/{schedule_id}/run")
def trigger_schedule_now(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    job_id = str(uuid.uuid4())
    create_job(job_id, schedule_id=schedule_id)

    prompt_text = _build_scheduled_prompt(schedule)

    thread = threading.Thread(
        target=run_agent_in_background,
        args=(job_id, prompt_text, None, None, schedule_id),
        daemon=True,
    )
    thread.start()

    return {
        "job_id": job_id,
        "schedule_id": schedule_id,
        "status": "processing",
        "message": "Manual trigger fired. Poll GET /result/{job_id} for status.",
    }


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /schedules/{schedule_id}/state â€” View current agent state
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DELETE /schedules/{schedule_id}/state â€” Reset agent state (re-process everything)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.delete("/schedules/{schedule_id}/state")
def reset_schedule_state(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    update_schedule(schedule_id, last_state=None)
    return {"schedule_id": schedule_id, "last_state": None, "message": "State cleared. Next run will start fresh."}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /files/{sandbox_id} â€” List files in sandbox
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /files/{sandbox_id}/download â€” Single file as base64
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Helpers
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Health check
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.get("/health")
def health():
    scheduled_jobs = len(scheduler.get_jobs())
    return {"status": "ok", "scheduled_jobs": scheduled_jobs}


# ──────────────────────────────────────────────
# Health check
# ──────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}
