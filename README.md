# Claude Code E2B Sandbox FastAPI Example

A FastAPI application that provides a REST API for running Claude Code in E2B sandboxes. This allows you to execute Claude Code commands remotely through HTTP requests, with support for session management and GitHub integration.

## Features

- **Remote Claude Code Execution**: Run Claude Code commands through HTTP API endpoints
- **Session Management**: Resume conversations across multiple requests
- **GitHub Integration**: Clone repositories and work with code in sandboxes
- **E2B Sandbox Environment**: Isolated, secure execution environment
- **MCP (Model Context Protocol) Support**: Enhanced capabilities through MCP servers

## Prerequisites

- Python 3.12 or higher
- E2B account and API key
- Anthropic API key (for Claude Code)
- GitHub Personal Access Token (optional, for repository access)

## Installation

1. Clone the repository:
```bash
git clone <your-repo-url>
cd claude-fastapi
```

2. Install dependencies using uv (recommended):
```bash
uv sync
```

Or using pip:
```bash
pip install -e .
```

## Environment Variables

Create a `.env` file in the project root with the following variables:

```env
# Required
ANTHROPIC_API_KEY=your_anthropic_api_key_here
E2B_API_KEY=your_e2b_api_key_here

# Optional
E2B_SANDBOX_TEMPLATE=claude-code-dev  # or claude-code for production
GITHUB_PAT=your_github_personal_access_token_here
```

## Building Templates

### Development Template

Build the development template:

```bash
cd template
python build_dev.py
```

This creates a template with:
- 1 CPU core
- 1024 MB memory
- Alias: `claude-code-dev`

### Production Template

Build the production template:

```bash
cd template
python build.py
```

This creates a template with:
- 1 CPU core
- 1024 MB memory
- Alias: `claude-code`

## Running the Application

1. Start the FastAPI server:
```bash
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`

2. Access the interactive API documentation:
```
http://localhost:8000/docs
```

## API Usage

### POST /chat

Send a request to execute Claude Code commands:

```bash
curl -X POST "http://localhost:8000/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Can you add GPT 3.5-Turbo to the list of models and open a pull request on GitHub",
    "repo": "https://github.com/e2b-dev/fragments"
  }'
```

#### Request Body

```json
{
  "prompt": "string",           // Required: The prompt/command for Claude Code
  "resume": "string",           // Optional: Session ID to resume conversation
  "repo": "string"              // Optional: GitHub repository URL to clone
}
```

#### Response

```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "duration_ms": 216401,
  "duration_api_ms": 216234,
  "num_turns": 81,
  "result": "Perfect! I have successfully completed all the tasks:\n\n✅ **All tasks completed!** The pull request has been created at: https://github.com/e2b-dev/fragments/pull/170\n\n## Summary\n\nI've successfully added GPT-3.5 Turbo to the model list and opened a pull request with the following changes:\n\n- **Added GPT-3.5 Turbo** to `/home/user/fragments/lib/models.json:108-114`\n- **Model configuration**:\n  - ID: `gpt-3.5-turbo`\n  - Provider: OpenAI\n  - Name: GPT-3.5 Turbo  \n  - Multimodal: false\n- **Validated** JSON syntax is correct\n- **Created branch** `add-gpt-3.5-turbo` \n- **Opened PR #170** with comprehensive description\n\nThe pull request is ready for review and includes all the necessary changes to integrate GPT-3.5 Turbo into the fragments application.",
  "session_id": "038b769b-4717-47ca-be02-2a49bd7da978",
  "total_cost_usd": 1.1459241,
  "usage": {
    "input_tokens": 300,
    "cache_creation_input_tokens": 77458,
    "cache_read_input_tokens": 2087724,
    "output_tokens": 13935,
    "server_tool_use": {
      "web_search_requests": 0
    },
    "service_tier": "standard"
  },
  "permission_denials": []
}
```

### Example Usage

1. **Initial Request**:
```bash
curl -X POST "http://localhost:8000/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Create a Python script that prints hello world",
    "repo": "https://github.com/example/my-project"
  }'
```

2. **Resume Conversation**:
```bash
curl -X POST "http://localhost:8000/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Now modify the script to accept a name parameter",
    "resume": "session_id_from_previous_response"
  }'
```

### MCP Servers

You can add your own MCP servers to Claude Code by editing the [template/.mcp.json](template/.mcp.json) file.

```json
{
  "servers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {
        "Authorization": "Bearer ${GITHUB_PAT}"
      }
    }
  }
}
```
