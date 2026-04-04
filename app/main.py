import json
import os
import re
import shlex
import uuid
import time
import asyncio
import threading
from typing import Optional
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from e2b import Sandbox
from supabase import create_client, Client
from mem0 import MemoryClient

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

MEM0_API_KEY = os.getenv("MEM0_API_KEY")
mem0_client = MemoryClient(api_key=MEM0_API_KEY) if MEM0_API_KEY else None
if mem0_client:
    print("[Mem0] Memory layer initialized.")
else:
    print("[Mem0] No API key found. Running without persistent memory.")

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
# System prompt for Tier 2 (sandbox/Claude Code CLI) — coding tasks only
system_prompt = """
GitHub PAT is already set in the environment GITHUB_PAT. The repository is already cloned in the sandbox and the working directory is the repository root.

You are an autonomous agent in the Clustor platform operating inside a sandboxed Linux environment. Your outputs are displayed directly to users in a professional dashboard interface that renders markdown.

ENVIRONMENT CAPABILITIES:
- Full Linux shell with Python 3, Node.js 24, git, ripgrep
- Code execution: write and run any Python, JavaScript, or shell script
- File creation: create any file type (documents, images, data files, code)
- Web browsing via Playwright MCP: you have a headless Chromium browser available
- GitHub access via GITHUB_PAT environment variable
- External tool access via Composio MCP (Gmail, Slack, Calendar, etc. if connected)

BROWSER AUTOMATION:
You have access to a Playwright MCP server that controls a headless Chromium browser. Use it to:
- Navigate to any website and extract information
- Fill out forms, click buttons, interact with web applications
- Take screenshots of pages for visual reference
- Log into authenticated services (if credentials are provided)
- Scrape data from websites that don't have APIs
- Monitor dashboards, check statuses, gather real-time data

The Playwright MCP tools use the browser's accessibility tree for fast, reliable interaction.
When browsing, prefer using element roles and labels over CSS selectors.
If a page requires scrolling to see all content, scroll and check for more.

You can also write Python scripts using the playwright library directly if you need more control:
```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://example.com")
    # ... interact with the page
    browser.close()
```

OUTPUT RULES:
- Never use emojis in your output. Use text labels like [HIGH], [MEDIUM], [LOW] instead.
- Structure your response with clear markdown: ## headers, **bold**, bullet lists, tables as appropriate.
- Be thorough — list every individual item with full details. Do not compress multiple items into one sentence.
- Write like a professional analyst delivering a briefing, not a chatbot sending a text message.
- Never dump raw data. Always interpret and organize it for a busy professional.
- When you take screenshots during browser tasks, mention what you captured and why.
- If a browser task fails, try an alternative approach before giving up.
SECURITY RULES:
- Never expose internal infrastructure details in your output: no sandbox IDs, container IPs, API keys, MCP server names, or backend service names.
- Never mention Composio, E2B, Railway, Playwright, HeadlessChrome, or any internal tooling by name in user-facing output.
- Never include raw HTTP headers, request metadata, User-Agent strings, or server trace IDs in deliverables.
- If a tool or service returns metadata, extract only the useful information and discard the rest.
- Present results as if you are a professional analyst who used whatever tools were necessary — the user doesn't need to know how you got the information, only what you found.
- Never include credentials, tokens, or authentication details in any output.
"""
sandbox_template = os.getenv("E2B_SANDBOX_TEMPLATE", "world-modal-agent-browser")
sandbox_timeout = 60 * 60  # 1 hour

# System prompt for Tier 1 scheduled tasks
TIER1_SYSTEM_PROMPT = """You are an autonomous agent on the Corpis platform. Your output is displayed in a professional dashboard that renders markdown.

RULES:
1. Begin with a # or ## markdown heading. No preamble.
2. Do NOT narrate your process. Just produce the deliverable.
3. Cite claims inline as [Source](URL). Real URLs only.
4. No emojis. Professional tone. Dense, data-rich.
5. After the deliverable, add '---' then a brief conversational summary (2-5 sentences).

SECURITY: Never expose internal infrastructure, tool names, API keys, or backend details."""

# System prompt for Tier 1 interactive (durable) tasks
TIER1_INTERACTIVE_SYSTEM_PROMPT = """You are a specialist research agent. Your output is a formal deliverable document.

CRITICAL RULES:
1. Your response MUST begin with a # or ## markdown heading. No text before the heading.
2. Do NOT narrate your process. Do NOT write 'I will...', 'Let me...', 'I found...'. Just produce the document.
3. Cite every factual claim inline as [Source](URL). Real URLs only.
4. No emojis. Professional tone. Dense, data-rich content.
5. After the complete deliverable, add a line with only '---', then write a BRIEF conversational summary (2-5 sentences, under 80 words).

SECURITY RULES:
- Never expose internal infrastructure details in your output.
- Never mention Composio, E2B, Railway, Playwright, HeadlessChrome, or any internal tooling by name.
- Never include raw HTTP headers, request metadata, or server trace IDs.
- Present results as if you are a professional analyst."""

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


class DurableExecuteRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    composio_mcp_url: Optional[str] = None
    composio_api_key: Optional[str] = None
    system_prompt: Optional[str] = None


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
    tier: Optional[str] = "api"  # "api" (Tier 1) or "sandbox" (Tier 2) — set by Task Architect


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    agent_prompt: Optional[str] = None
    cron_expression: Optional[str] = None
    enabled: Optional[bool] = None


# ------------------------------------------
# Supabase Helpers - Jobs
# ------------------------------------------
def create_job(job_id: str, schedule_id: Optional[str] = None):
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
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    db.table("agent_jobs").update(fields).eq("job_id", job_id).execute()


def get_job(job_id: str):
    result = db.table("agent_jobs").select("*").eq("job_id", job_id).execute()
    if result.data and len(result.data) > 0:
        return result.data[0]
    return None


def save_session_sandbox(session_id: str, sandbox_id: str):
    db.table("session_sandboxes").upsert({
        "session_id": session_id,
        "sandbox_id": sandbox_id,
    }).execute()


def get_sandbox_for_session(session_id: str) -> Optional[str]:
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
    result = db.table("schedules").select("*").eq("enabled", True).execute()
    return result.data or []


def get_schedule(schedule_id: str):
    result = db.table("schedules").select("*").eq("id", schedule_id).execute()
    if result.data and len(result.data) > 0:
        return result.data[0]
    return None


def update_schedule(schedule_id: str, **fields):
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    db.table("schedules").update(fields).eq("id", schedule_id).execute()


def get_agent_state(schedule_id: str) -> dict:
    result = db.table("schedules").select("last_state").eq("id", schedule_id).execute()
    if result.data and len(result.data) > 0:
        return result.data[0].get("last_state") or {}
    return {}


def save_agent_state(schedule_id: str, state: dict):
    update_schedule(
        schedule_id,
        last_state=state,
        last_run_at=datetime.now(timezone.utc).isoformat(),
    )


def record_agent_run(schedule_id: str, job_id: str, status: str, summary: Optional[str] = None, error: Optional[str] = None, result_type: Optional[str] = None):
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
# Mem0 Persistent Memory
# ------------------------------------------
def recall_memories(user_id: str, query: str, limit: int = 5) -> str:
    if not mem0_client or not user_id:
        return ""
    try:
        results = mem0_client.search(query=query, user_id=user_id, limit=limit)
        memories = results.get("results", []) if isinstance(results, dict) else results
        if not memories:
            return ""
        memory_lines = []
        for entry in memories:
            mem_text = entry.get("memory", "") if isinstance(entry, dict) else str(entry)
            if mem_text:
                memory_lines.append(f"- {mem_text}")
        return "\n".join(memory_lines) if memory_lines else ""
    except Exception as e:
        print(f"[Mem0] Recall failed for user {user_id}: {e}")
        return ""


def store_memories(user_id: str, user_message: str, assistant_message: str):
    if not mem0_client or not user_id:
        return
    try:
        mem0_client.add(
            [
                {"role": "user", "content": user_message[:2000]},
                {"role": "assistant", "content": assistant_message[:2000]},
            ],
            user_id=user_id,
        )
        print(f"[Mem0] Stored memories for user {user_id}")
    except Exception as e:
        print(f"[Mem0] Store failed for user {user_id}: {e}")


# ------------------------------------------
# Composio MCP Session Refresh
# ------------------------------------------
def refresh_composio_mcp_url(entity_id: str, api_key: str) -> Optional[str]:
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


# ===========================================================================
# DURABLE EXECUTION — Agent Steps Event Stream
# Every tool call, state change, and checkpoint is logged to agent_steps.
# Enables crash recovery, live activity timeline, and SSE streaming.
# ===========================================================================

def _emit_step(job_id: str, session_id: str = None, agent_id: str = None,
               step_number: int = 0, step_type: str = "state", **kwargs):
    """Write one event to agent_steps table."""
    row = {
        "job_id": job_id,
        "session_id": session_id,
        "agent_id": agent_id,
        "step_number": step_number,
        "step_type": step_type,
    }
    for key in ("tool_name", "tool_input", "token_text", "state",
                "messages_snapshot", "accumulated_output", "iteration",
                "token_count_in", "token_count_out", "cost_usd", "error"):
        if key in kwargs and kwargs[key] is not None:
            row[key] = kwargs[key]
    try:
        db.table("agent_steps").insert(row).execute()
    except Exception as e:
        print(f"[Steps] Failed to write step: {e}")


def _load_latest_checkpoint(job_id: str) -> dict | None:
    """Load the most recent checkpoint for a job (for resume after crash)."""
    try:
        result = (
            db.table("agent_steps")
            .select("*")
            .eq("job_id", job_id)
            .eq("step_type", "checkpoint")
            .order("step_number", desc=True)
            .limit(1)
            .execute()
        )
        if result.data and len(result.data) > 0:
            return result.data[0]
    except Exception as e:
        print(f"[Steps] Failed to load checkpoint: {e}")
    return None


def _heartbeat(job_id: str, iteration: int):
    """Update job record so frontend knows the agent is alive."""
    try:
        db.table("agent_jobs").update({
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "current_iteration": iteration,
        }).eq("job_id", job_id).execute()
    except Exception:
        pass


def _call_anthropic_with_retry(headers: dict, body: dict, max_retries: int = 3):
    """Call Anthropic API with exponential backoff retry on transient errors."""
    for attempt in range(max_retries):
        try:
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=body,
                timeout=120,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = (2 ** attempt) + 1
                print(f"[Tier1-Durable] Retry {attempt + 1}/{max_retries} after {resp.status_code}, waiting {wait}s")
                time.sleep(wait)
                continue
            if resp.is_success:
                return resp
            print(f"[Tier1-Durable] API error {resp.status_code}: {resp.text[:300]}")
            return None
        except Exception as e:
            wait = (2 ** attempt) + 1
            print(f"[Tier1-Durable] Network error, retry {attempt + 1}/{max_retries}: {e}")
            time.sleep(wait)
    return None


def _safe_truncate_input(tool_input, max_len: int = 2000) -> dict:
    """Truncate large tool input values for DB storage."""
    if not isinstance(tool_input, dict):
        return tool_input if isinstance(tool_input, dict) else {}
    truncated = {}
    for k, v in tool_input.items():
        if isinstance(v, str) and len(v) > max_len:
            truncated[k] = v[:max_len] + "...[truncated]"
        else:
            truncated[k] = v
    return truncated


# ------------------------------------------
# Tier 1 Durable: Checkpointed agent loop
# Runs on Railway with no timeout. Writes every event to DB.
# Resumable after crash/redeploy via checkpoint.
# ------------------------------------------
def run_tier1_durable(
    job_id: str,
    prompt_text: str = "",
    session_id: str = None,
    agent_id: str = None,
    schedule_id: str = None,
    composio_mcp_url: str = None,
    composio_api_key: str = None,
    system_prompt_override: str = None,
):
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        use_prompt = system_prompt_override or TIER1_INTERACTIVE_SYSTEM_PROMPT

        # ── Resume from checkpoint if available ──
        checkpoint = _load_latest_checkpoint(job_id)
        if checkpoint and checkpoint.get("messages_snapshot"):
            messages = checkpoint["messages_snapshot"]
            accumulated_output = checkpoint.get("accumulated_output") or ""
            start_iteration = (checkpoint.get("iteration") or 0) + 1
            total_input_tokens = checkpoint.get("token_count_in") or 0
            total_output_tokens = checkpoint.get("token_count_out") or 0
            print(f"[Tier1-Durable] Resuming job {job_id} from iteration {start_iteration}")
            _emit_step(job_id, session_id, agent_id, start_iteration * 10, "state",
                       state="working")
        else:
            messages = [{"role": "user", "content": prompt_text}]
            accumulated_output = ""
            start_iteration = 0
            total_input_tokens = 0
            total_output_tokens = 0
            _emit_step(job_id, session_id, agent_id, 0, "state", state="assigned")
            _emit_step(job_id, session_id, agent_id, 1, "state", state="working")

        # ── Build request ──
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        request_body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16000,
            "system": use_prompt,
        }

        # Add Composio MCP if available
        if composio_mcp_url:
            headers["anthropic-beta"] = "mcp-client-2025-11-20"
            mcp_server = {
                "type": "url",
                "url": composio_mcp_url,
                "name": "composio",
            }
            if composio_api_key and "api_key=" not in composio_mcp_url:
                mcp_server["authorization_token"] = composio_api_key
            request_body["mcp_servers"] = [mcp_server]
            request_body["tools"] = [
                {"type": "mcp_toolset", "mcp_server_name": "composio"},
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 10},
            ]
        else:
            request_body["tools"] = [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 10},
            ]

        max_iterations = 50
        step_counter = 2 if start_iteration == 0 else (start_iteration * 10 + 1)

        for iteration in range(start_iteration, max_iterations):
            request_body["messages"] = messages

            # ── Heartbeat ──
            _heartbeat(job_id, iteration)

            # ── API call with retry ──
            resp = _call_anthropic_with_retry(headers, request_body)
            if resp is None:
                error_msg = "Anthropic API call failed after retries"
                step_counter += 1
                _emit_step(job_id, session_id, agent_id, step_counter, "error",
                           error=error_msg, iteration=iteration)
                update_job(job_id, status="error", error=error_msg)
                if schedule_id:
                    record_agent_run(schedule_id, job_id, "error", error=error_msg)
                return

            result = resp.json()

            # ── Track tokens ──
            usage = result.get("usage", {})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

            content_blocks = result.get("content", [])

            # ── Process text blocks ──
            text_blocks = [b for b in content_blocks if b.get("type") == "text"]
            iteration_text = "\n".join(b.get("text", "") for b in text_blocks)
            if iteration_text:
                accumulated_output += iteration_text
                step_counter += 1
                _emit_step(job_id, session_id, agent_id, step_counter, "token",
                           token_text=iteration_text[:1000], iteration=iteration)
                step_counter += 1
                _emit_step(job_id, session_id, agent_id, step_counter, "state",
                           state="drafting")

            # ── Process tool calls ──
            tool_blocks = [b for b in content_blocks
                           if b.get("type") in ("tool_use", "mcp_tool_use")]

            for tb in tool_blocks:
                tool_name = tb.get("name", "unknown")
                tool_input = tb.get("input", {})
                step_counter += 1

                if tool_name == "web_search" or "search" in tool_name.lower():
                    _emit_step(job_id, session_id, agent_id, step_counter, "state",
                               state="searching")
                    step_counter += 1

                _emit_step(job_id, session_id, agent_id, step_counter, "tool_start",
                           tool_name=tool_name,
                           tool_input=_safe_truncate_input(tool_input),
                           iteration=iteration)

            # ── Check stop reason ──
            stop_reason = result.get("stop_reason", "end_turn")

            if stop_reason == "end_turn":
                step_counter += 1
                _emit_step(job_id, session_id, agent_id, step_counter, "state",
                           state="finalizing")
                print(f"[Tier1-Durable] Complete after {iteration + 1} iterations, "
                      f"{len(accumulated_output)} chars")
                break

            # ── Continue tool loop ──
            messages.append({"role": "assistant", "content": content_blocks})

            if tool_blocks:
                tool_results = []
                for block in tool_blocks:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("id", ""),
                        "content": "Executed. Continue with the task.",
                    })
                    step_counter += 1
                    _emit_step(job_id, session_id, agent_id, step_counter, "tool_end",
                               tool_name=block.get("name", "unknown"),
                               iteration=iteration)

                messages.append({"role": "user", "content": tool_results})
                print(f"[Tier1-Durable] Iteration {iteration + 1}: "
                      f"{len(tool_blocks)} tool calls")
            else:
                break

            # ── CHECKPOINT — save full state for crash recovery ──
            step_counter += 1
            _emit_step(job_id, session_id, agent_id, step_counter, "checkpoint",
                       messages_snapshot=messages,
                       accumulated_output=accumulated_output,
                       iteration=iteration,
                       token_count_in=total_input_tokens,
                       token_count_out=total_output_tokens)

            step_counter += 1
            _emit_step(job_id, session_id, agent_id, step_counter, "state",
                       state="working")

        # ── Calculate cost ──
        input_cost = (total_input_tokens / 1_000_000) * 3
        output_cost = (total_output_tokens / 1_000_000) * 15
        total_cost = round(input_cost + output_cost, 4)

        print(f"[Tier1-Durable] Tokens: {total_input_tokens} in, "
              f"{total_output_tokens} out, ${total_cost}")

        # ── Mark complete ──
        step_counter += 1
        _emit_step(job_id, session_id, agent_id, step_counter, "state",
                   state="complete",
                   token_count_in=total_input_tokens,
                   token_count_out=total_output_tokens,
                   cost_usd=total_cost)

        result_payload = {
            "result": accumulated_output,
            "total_cost_usd": total_cost,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "iterations": iteration + 1,
            "tier": "api",
        }

        update_job(job_id, status="complete", result=result_payload)

        if schedule_id:
            new_state = _extract_agent_state(accumulated_output)
            if new_state:
                save_agent_state(schedule_id, new_state)
            record_agent_run(
                schedule_id, job_id, "complete",
                summary=accumulated_output[:2000] if accumulated_output else None,
                result_type=_extract_result_type(accumulated_output),
            )
            store_memories(schedule_id, prompt_text[:2000], accumulated_output[:2000])

    except Exception as e:
        print(f"[Tier1-Durable] Error job {job_id}: {e}")
        _emit_step(job_id, session_id, agent_id, 0, "error", error=str(e))
        update_job(job_id, status="error", error=str(e))
        if schedule_id:
            record_agent_run(schedule_id, job_id, "error", error=str(e))


# ------------------------------------------
# Tier 1: Direct Claude API (original — used by scheduler)
# ------------------------------------------
def run_tier1_agent(
    job_id: str,
    prompt_text: str,
    schedule_id: str,
    composio_mcp_url: Optional[str] = None,
    composio_api_key: Optional[str] = None,
):
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY")

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        request_body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16000,
            "system": TIER1_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt_text}],
        }

        # Add Composio MCP — gives Claude access to Gmail, Slack, GitHub, etc.
        if composio_mcp_url:
            headers["anthropic-beta"] = "mcp-client-2025-11-20"
            mcp_server = {
                "type": "url",
                "url": composio_mcp_url,
                "name": "composio",
            }
            if composio_api_key and "api_key=" not in composio_mcp_url:
                mcp_server["authorization_token"] = composio_api_key
            request_body["mcp_servers"] = [mcp_server]
            request_body["tools"] = [
                {"type": "mcp_toolset", "mcp_server_name": "composio"},
            ]

        print(f"[Tier1] Starting agent for schedule {schedule_id}, job {job_id}, mcp={bool(composio_mcp_url)}")

        memories_str = recall_memories(schedule_id, prompt_text)
        if memories_str:
            prompt_text = f"""WHAT I KNOW ABOUT YOU (from previous interactions):
{memories_str}

Use this context naturally. Don't mention that you "remember" things — just apply the knowledge.

---

{prompt_text}"""

        messages = [{"role": "user", "content": prompt_text}]
        total_input_tokens = 0
        total_output_tokens = 0
        max_iterations = 25
        final_text = ""
        iteration = 0

        for iteration in range(max_iterations):
            request_body["messages"] = messages

            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=request_body,
                timeout=120,
            )

            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(3)
                resp = httpx.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=request_body,
                    timeout=120,
                )

            if not resp.is_success:
                error_text = resp.text
                print(f"[Tier1] API error iteration {iteration}: {resp.status_code}")
                update_job(job_id, status="error", error=f"API error [{resp.status_code}]: {error_text[:500]}")
                record_agent_run(schedule_id, job_id, "error", error=error_text[:500])
                return

            result = resp.json()

            usage = result.get("usage", {})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

            content_blocks = result.get("content", [])
            text_blocks = [b for b in content_blocks if b.get("type") == "text"]
            iteration_text = "\n".join(b.get("text", "") for b in text_blocks)
            if iteration_text:
                final_text += iteration_text

            stop_reason = result.get("stop_reason", "end_turn")

            if stop_reason == "end_turn":
                print(f"[Tier1] Complete after {iteration + 1} iterations, {len(final_text)} chars")
                break

            messages.append({"role": "assistant", "content": content_blocks})

            tool_blocks = [b for b in content_blocks if b.get("type") in ("tool_use", "mcp_tool_use")]
            if tool_blocks:
                tool_results = []
                for block in tool_blocks:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("id", ""),
                        "content": "Executed. Continue with the task.",
                    })
                messages.append({"role": "user", "content": tool_results})
                print(f"[Tier1] Iteration {iteration + 1}: {len(tool_blocks)} tool calls")
            else:
                break

        input_cost = (total_input_tokens / 1_000_000) * 3
        output_cost = (total_output_tokens / 1_000_000) * 15
        total_cost = round(input_cost + output_cost, 4)

        print(f"[Tier1] Tokens: {total_input_tokens} in, {total_output_tokens} out, ${total_cost}")

        result_payload = {
            "result": final_text,
            "total_cost_usd": total_cost,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "iterations": iteration + 1,
            "tier": "api",
        }

        update_job(job_id, status="complete", result=result_payload)

        new_state = _extract_agent_state(final_text)
        if new_state:
            save_agent_state(schedule_id, new_state)

        result_type = _extract_result_type(final_text)
        record_agent_run(
            schedule_id, job_id, "complete",
            summary=final_text[:2000] if final_text else None,
            result_type=result_type,
        )

        store_memories(schedule_id, prompt_text[:2000], final_text[:2000])

    except Exception as e:
        print(f"[Tier1] Error job {job_id}: {e}")
        update_job(job_id, status="error", error=str(e))
        record_agent_run(schedule_id, job_id, "error", error=str(e))


# ------------------------------------------
# Tier 2: Sandbox Agent Runner (Claude Code CLI)
# ONLY for tasks needing code execution, file creation, or browser automation.
# ------------------------------------------
def run_agent_in_sandbox(
    job_id: str,
    prompt_text: str,
    repo: Optional[str],
    session: Optional[str],
    schedule_id: Optional[str] = None,
    composio_mcp_url: Optional[str] = None,
    composio_api_key: Optional[str] = None,
):
    try:
        sandbox_envs = {
            "GITHUB_PAT": os.getenv("GITHUB_PAT", ""),
            "CONTEXT7_API_KEY": os.getenv("CONTEXT7_API_KEY", ""),
            "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
        }
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
                sandbox.commands.run(f"git clone {repo} && cd {repo.split('/')[-1]}")
        else:
            sandbox_id = get_sandbox_for_session(session)
            if not sandbox_id:
                update_job(job_id, status="error", error=f"No sandbox found for session {session}")
                if schedule_id:
                    record_agent_run(schedule_id, job_id, "error", error=f"No sandbox for session {session}")
                return
            sandbox = Sandbox.connect(sandbox_id=sandbox_id)

        active_sandboxes[sandbox.sandbox_id] = sandbox
        update_job(job_id, sandbox_id=sandbox.sandbox_id)

        mcp_config = {
            "mcpServers": {
                "context7": {
                    "type": "http",
                    "url": "https://mcp.context7.com/mcp",
                    "headers": {
                        "Authorization": f"Bearer {sandbox_envs.get('CONTEXT7_API_KEY', '')}"
                    }
                },
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp@latest", "--headless"]
                }
            }
        }
        if composio_mcp_url:
            mcp_config["mcpServers"]["composio"] = {
                "type": "http",
                "url": composio_mcp_url,
                "headers": {"Authorization": f"Bearer {composio_api_key or ''}"}
            }
        mcp_json_str = json.dumps(mcp_config, indent=2)
        sandbox.commands.run(f"echo {shlex.quote(mcp_json_str)} > /home/user/.mcp.json", timeout=10)
        sandbox.commands.run(f"echo {shlex.quote(mcp_json_str)} > .mcp.json", timeout=10)

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

        safe_prompt = json.dumps(prompt_text)
        sandbox.commands.run(f"echo {shlex.quote(safe_prompt)} > /tmp/agent_prompt.txt", timeout=30)

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

        if "session_id" in claude_response:
            save_session_sandbox(claude_response["session_id"], sandbox.sandbox_id)

        claude_response["sandbox_id"] = sandbox.sandbox_id

        update_job(
            job_id, status="complete",
            result=claude_response,
            session_id=claude_response.get("session_id"),
        )

        if schedule_id:
            result_text = claude_response.get("result", "") or ""
            new_state = _extract_agent_state(result_text)
            if new_state:
                save_agent_state(schedule_id, new_state)
            result_type = _extract_result_type(result_text)
            record_agent_run(
                schedule_id, job_id, "complete",
                summary=result_text[:2000] if result_text else None,
                result_type=result_type,
            )

    except Exception as e:
        update_job(job_id, status="error", error=str(e))
        if schedule_id:
            record_agent_run(schedule_id, job_id, "error", error=str(e))


# ------------------------------------------
# State & Result Type Extraction
# ------------------------------------------
def _extract_agent_state(result_text: str) -> Optional[dict]:
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
    match = re.search(r'__result_type__:\s*(\w+)', result_text)
    if match:
        return match.group(1)
    return "task_update"


# ------------------------------------------
# Task Tier Detection for Scheduled Tasks
# ------------------------------------------
def _needs_sandbox(prompt: str) -> bool:
    sandbox_patterns = [
        r'\b(write|create|build|generate)\s+(code|script|program|app|website|application)\b',
        r'\b(execute|run)\s+(code|script|python|javascript|bash)\b',
        r'\b(scrape|crawl|automate\s+browser|playwright|selenium)\b',
        r'\b(create|generate|build)\s+(pdf|docx|xlsx|pptx|spreadsheet)\b',
        r'\b(git\s+clone|npm\s+install|pip\s+install)\b',
        r'\b(deploy|compile|build\s+and\s+deploy)\b',
    ]
    prompt_lower = prompt.lower()
    for pattern in sandbox_patterns:
        if re.search(pattern, prompt_lower):
            return True
    return False


# ------------------------------------------
# Scheduled Prompt Builder
# ------------------------------------------
def _build_scheduled_prompt(schedule: dict) -> str:
    base_prompt = schedule["agent_prompt"]
    last_state = schedule.get("last_state") or {}

    state_block = ""
    if last_state:
        state_block = f"""

PREVIOUS RUN STATE (skip these items, only report NEW ones):
{json.dumps(last_state, indent=2)}
"""

    memories_str = recall_memories(schedule.get("composio_entity_id", schedule["id"]), base_prompt)
    memory_block = ""
    if memories_str:
        memory_block = f"""
USER CONTEXT (from previous interactions):
{memories_str}
"""

    return f"""You are an autonomous agent running a scheduled task on the Clustor platform. Your output is displayed directly in a professional dashboard UI that renders markdown beautifully.
{memory_block}
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


# ------------------------------------------
# Scheduler
# ------------------------------------------
scheduler = BackgroundScheduler(timezone="UTC")


def _run_scheduled_agent(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule or not schedule.get("enabled"):
        return

    job_id = str(uuid.uuid4())
    create_job(job_id, schedule_id=schedule_id)

    prompt_text = _build_scheduled_prompt(schedule)

    composio_mcp_url = schedule.get("composio_mcp_url")
    composio_api_key = schedule.get("composio_api_key")
    composio_entity_id = schedule.get("composio_entity_id")

    if composio_entity_id and composio_api_key:
        fresh_url = refresh_composio_mcp_url(composio_entity_id, composio_api_key)
        if fresh_url:
            composio_mcp_url = fresh_url
            update_schedule(schedule_id, composio_mcp_url=fresh_url)

    stored_tier = schedule.get("tier", "")
    if stored_tier == "sandbox":
        use_sandbox = True
    elif stored_tier == "api":
        use_sandbox = False
    else:
        use_sandbox = _needs_sandbox(schedule.get("agent_prompt", ""))

    if use_sandbox:
        print(f"[Scheduler] {schedule_id} -> Tier 2 (sandbox)")
        thread = threading.Thread(
            target=run_agent_in_sandbox,
            args=(job_id, prompt_text, None, None, schedule_id),
            kwargs={"composio_mcp_url": composio_mcp_url, "composio_api_key": composio_api_key},
            daemon=True,
        )
    else:
        print(f"[Scheduler] {schedule_id} -> Tier 1 (direct API)")
        thread = threading.Thread(
            target=run_tier1_agent,
            args=(job_id, prompt_text, schedule_id),
            kwargs={"composio_mcp_url": composio_mcp_url, "composio_api_key": composio_api_key},
            daemon=True,
        )
    thread.start()


def _load_schedules_into_scheduler():
    schedules = get_all_enabled_schedules()
    for schedule in schedules:
        _register_schedule(schedule)
    print(f"[Scheduler] Loaded {len(schedules)} schedule(s) from Supabase.")


def _register_schedule(schedule: dict):
    job_id = f"schedule_{schedule['id']}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if schedule.get("enabled"):
        scheduler.add_job(
            _run_scheduled_agent,
            trigger=CronTrigger.from_crontab(schedule["cron_expression"], timezone="UTC"),
            id=job_id,
            args=[schedule["id"]],
            replace_existing=True,
            misfire_grace_time=300,
        )


def _unregister_schedule(schedule_id: str):
    job_id = f"schedule_{schedule_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


# ------------------------------------------
# App Lifecycle
# ------------------------------------------
@app.on_event("startup")
def startup_event():
    _load_schedules_into_scheduler()
    scheduler.start()
    print("[Scheduler] Started.")
    # Resume any jobs that were in-progress when Railway restarted
    try:
        incomplete = (
            db.table("agent_jobs")
            .select("job_id, tier")
            .eq("status", "processing")
            .execute()
        )
        jobs = incomplete.data or []
        resumed = 0
        for job in jobs:
            cp = _load_latest_checkpoint(job["job_id"])
            if cp and cp.get("messages_snapshot"):
                thread = threading.Thread(
                    target=run_tier1_durable,
                    args=(job["job_id"],),
                    daemon=True,
                )
                thread.start()
                resumed += 1
                print(f"[Startup] Resumed job {job['job_id']}")
            else:
                update_job(job["job_id"], status="error",
                           error="Job lost during server restart (no checkpoint)")
        print(f"[Startup] Resume check: {resumed} resumed, {len(jobs)} total incomplete")
    except Exception as e:
        print(f"[Startup] Resume check failed: {e}")


@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown(wait=False)
    print("[Scheduler] Stopped.")


# ------------------------------------------
# POST /chat - Tier 2 sandbox for interactive coding tasks
# ------------------------------------------
@app.post("/chat/{session}")
@app.post("/chat")
def prompt(prompt: ClaudePrompt, session: Optional[str] = None):
    job_id = str(uuid.uuid4())
    create_job(job_id)
    thread = threading.Thread(
        target=run_agent_in_sandbox,
        args=(job_id, prompt.prompt, prompt.repo, session),
        kwargs={"composio_mcp_url": prompt.composio_mcp_url, "composio_api_key": prompt.composio_api_key},
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id, "status": "processing", "message": "Agent started. Poll GET /result/{job_id} for status."}


# ------------------------------------------
# GET /result/{job_id}
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


# ===========================================================================
# DURABLE EXECUTION ENDPOINTS
# ===========================================================================

# ------------------------------------------
# POST /execute — Durable Tier 1 execution
# ------------------------------------------
@app.post("/execute")
def execute_durable(body: DurableExecuteRequest):
    job_id = str(uuid.uuid4())
    create_job(job_id)

    try:
        updates = {"tier": "api"}
        if body.session_id:
            updates["session_id_ref"] = body.session_id
        if body.agent_id:
            updates["agent_id"] = body.agent_id
        db.table("agent_jobs").update(updates).eq("job_id", job_id).execute()
    except Exception:
        pass

    thread = threading.Thread(
        target=run_tier1_durable,
        args=(job_id,),
        kwargs={
            "prompt_text": body.prompt,
            "session_id": body.session_id,
            "agent_id": body.agent_id,
            "composio_mcp_url": body.composio_mcp_url,
            "composio_api_key": body.composio_api_key,
            "system_prompt_override": body.system_prompt,
        },
        daemon=True,
    )
    thread.start()

    return {
        "job_id": job_id,
        "status": "processing",
        "message": "Durable agent started. Poll /result/{job_id} or subscribe to /stream/{job_id}.",
    }


# ------------------------------------------
# GET /stream/{job_id} — SSE stream of agent steps
# ------------------------------------------
@app.get("/stream/{job_id}")
async def stream_agent_steps(job_id: str):
    async def event_generator():
        last_step = -1
        stale_count = 0
        max_stale = 300  # 5 min with no new steps

        while True:
            try:
                result = (
                    db.table("agent_steps")
                    .select("step_number, step_type, tool_name, tool_input, "
                            "token_text, state, error, iteration, "
                            "token_count_in, token_count_out, cost_usd, created_at")
                    .eq("job_id", job_id)
                    .gt("step_number", last_step)
                    .order("step_number")
                    .limit(50)
                    .execute()
                )

                steps = result.data or []

                if steps:
                    stale_count = 0
                    for step in steps:
                        yield f"data: {json.dumps(step)}\n\n"
                        last_step = max(last_step, step["step_number"])
                else:
                    stale_count += 1

                job = get_job(job_id)
                if job and job.get("status") in ("complete", "error"):
                    yield f"data: {json.dumps({'step_type': 'done', 'status': job['status']})}\n\n"
                    break

                if stale_count >= max_stale:
                    yield f"data: {json.dumps({'step_type': 'timeout', 'error': 'No activity for 5 minutes'})}\n\n"
                    break

            except Exception as e:
                yield f"data: {json.dumps({'step_type': 'error', 'error': str(e)})}\n\n"
                break

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------
# GET /steps/{job_id} — Fetch all steps (non-streaming)
# ------------------------------------------
@app.get("/steps/{job_id}")
def get_steps(job_id: str, after: int = -1, limit: int = 100):
    result = (
        db.table("agent_steps")
        .select("step_number, step_type, tool_name, tool_input, "
                "token_text, state, error, iteration, "
                "token_count_in, token_count_out, cost_usd, created_at")
        .eq("job_id", job_id)
        .gt("step_number", after)
        .order("step_number")
        .limit(limit)
        .execute()
    )
    return {"job_id": job_id, "steps": result.data or []}


# ------------------------------------------
# POST /resume — Resume incomplete jobs after redeploy
# ------------------------------------------
@app.post("/resume")
def resume_incomplete_jobs():
    try:
        result = (
            db.table("agent_jobs")
            .select("job_id, tier")
            .eq("status", "processing")
            .execute()
        )
        jobs = result.data or []
        resumed = 0

        for job in jobs:
            jid = job["job_id"]
            checkpoint = _load_latest_checkpoint(jid)
            if not checkpoint or not checkpoint.get("messages_snapshot"):
                update_job(jid, status="error",
                           error="Job lost during server restart (no checkpoint)")
                continue

            thread = threading.Thread(
                target=run_tier1_durable,
                args=(jid,),
                daemon=True,
            )
            thread.start()
            resumed += 1
            print(f"[Resume] Resumed job {jid}")

        return {"resumed": resumed, "total_incomplete": len(jobs)}
    except Exception as e:
        return {"error": str(e)}


# ------------------------------------------
# Schedule CRUD
# ------------------------------------------
@app.post("/schedules")
def create_schedule_endpoint(body: ScheduleCreate):
    result = db.table("schedules").insert({
        "name": body.name,
        "agent_prompt": body.agent_prompt,
        "cron_expression": body.cron_expression,
        "enabled": body.enabled,
        "sandbox_template": body.sandbox_template,
        "composio_entity_id": body.composio_entity_id,
        "composio_api_key": body.composio_api_key,
        "composio_mcp_url": body.composio_mcp_url,
        "tier": body.tier or "api",
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
        "message": f"Schedule created. Next run: {_next_run_time(schedule['id'])}",
    }


@app.get("/schedules")
def list_schedules():
    result = db.table("schedules").select("*").order("created_at", desc=True).execute()
    schedules = result.data or []
    for s in schedules:
        s["next_run_at"] = _next_run_time(s["id"])
    return {"schedules": schedules}


@app.get("/schedules/{schedule_id}")
def get_schedule_detail(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    runs = (
        db.table("agent_runs").select("*")
        .eq("schedule_id", schedule_id)
        .order("ran_at", desc=True).limit(10).execute()
    )
    schedule["next_run_at"] = _next_run_time(schedule_id)
    schedule["recent_runs"] = runs.data or []
    return schedule


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


@app.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    _unregister_schedule(schedule_id)
    db.table("schedules").delete().eq("id", schedule_id).execute()
    return {"id": schedule_id, "deleted": True}


@app.post("/schedules/{schedule_id}/run")
def trigger_schedule_now(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    job_id = str(uuid.uuid4())
    create_job(job_id, schedule_id=schedule_id)
    prompt_text = _build_scheduled_prompt(schedule)

    composio_mcp_url = schedule.get("composio_mcp_url")
    composio_api_key = schedule.get("composio_api_key")
    composio_entity_id = schedule.get("composio_entity_id")

    if composio_entity_id and composio_api_key:
        fresh_url = refresh_composio_mcp_url(composio_entity_id, composio_api_key)
        if fresh_url:
            composio_mcp_url = fresh_url

    stored_tier = schedule.get("tier", "")
    if stored_tier == "sandbox":
        use_sandbox = True
    elif stored_tier == "api":
        use_sandbox = False
    else:
        use_sandbox = _needs_sandbox(schedule.get("agent_prompt", ""))

    if use_sandbox:
        print(f"[Manual] {schedule_id} -> Tier 2 (sandbox)")
        thread = threading.Thread(
            target=run_agent_in_sandbox,
            args=(job_id, prompt_text, None, None, schedule_id),
            kwargs={"composio_mcp_url": composio_mcp_url, "composio_api_key": composio_api_key},
            daemon=True,
        )
    else:
        print(f"[Manual] {schedule_id} -> Tier 1 (direct API)")
        thread = threading.Thread(
            target=run_tier1_agent,
            args=(job_id, prompt_text, schedule_id),
            kwargs={"composio_mcp_url": composio_mcp_url, "composio_api_key": composio_api_key},
            daemon=True,
        )
    thread.start()
    return {
        "job_id": job_id, "schedule_id": schedule_id,
        "status": "processing", "tier": "sandbox" if use_sandbox else "api",
        "message": "Manual trigger fired. Poll GET /result/{job_id} for status.",
    }


@app.get("/schedules/{schedule_id}/state")
def get_schedule_state(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"schedule_id": schedule_id, "last_state": schedule.get("last_state") or {}, "last_run_at": schedule.get("last_run_at")}


@app.delete("/schedules/{schedule_id}/state")
def reset_schedule_state(schedule_id: str):
    schedule = get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    update_schedule(schedule_id, last_state=None)
    return {"schedule_id": schedule_id, "last_state": None, "message": "State cleared."}


# ------------------------------------------
# File endpoints (sandbox only)
# ------------------------------------------
@app.get("/files/{sandbox_id}")
def list_files(sandbox_id: str):
    sandbox = _get_sandbox(sandbox_id)
    result = sandbox.commands.run(
        'find /home/user -type f -not -path "*/\\.*" -not -path "*/node_modules/*" -not -path "*/__pycache__/*" -not -name "*.pyc" 2>/dev/null | head -100',
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
        files.append(FileInfo(path=filepath, name=name, size=size, extension=ext))
    return {"sandbox_id": sandbox_id, "files": [f.dict() for f in files]}


@app.get("/files/{sandbox_id}/download")
def download_file(sandbox_id: str, path: str):
    sandbox = _get_sandbox(sandbox_id)
    result = sandbox.commands.run(f'base64 -w 0 "{path}" 2>/dev/null', timeout=30)
    if not result.stdout or result.exit_code != 0:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    name = path.split("/")[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {"path": path, "name": name, "extension": ext, "content_base64": result.stdout.strip(), "mime_type": _get_mime_type(ext)}


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
    job = scheduler.get_job(f"schedule_{schedule_id}")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def _get_mime_type(ext: str) -> str:
    mime_map = {
        "pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv",
        "html": "text/html", "css": "text/css", "js": "application/javascript", "ts": "application/typescript",
        "json": "application/json", "md": "text/markdown", "txt": "text/plain", "py": "text/x-python",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
        "svg": "image/svg+xml", "webp": "image/webp", "zip": "application/zip",
    }
    return mime_map.get(ext, "application/octet-stream")


# ------------------------------------------
# Health check
# ------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "scheduled_jobs": len(scheduler.get_jobs())}
