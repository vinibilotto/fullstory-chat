# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

The project ships a self-contained Python 3.12 runtime at `python-embed/` — use it directly, not the system Python.

```bat
cd fullstory-chat
..\python-embed\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

`start.bat` is a wrapper that requires `ANTHROPIC_API_KEY` to be set in the environment. Running uvicorn directly bypasses that check and uses the OAuth fallback automatically.

App runs at `http://localhost:8000`.

## Architecture

This is a single-file FastAPI backend (`fullstory-chat/app.py`) that bridges the browser, the Claude API, and the FullStory MCP server.

**Request flow:**
1. Browser (`index.html`) sends conversation history to `POST /api/chat`
2. Backend calls Claude (`claude-haiku-4-5-20251001`) with FullStory tools loaded
3. When Claude requests a tool, the backend calls FullStory's MCP server via JSON-RPC 2.0 at `https://api.fullstory.com/mcp/fullstory`
4. Tool results are fed back to Claude; the loop continues until `stop_reason != "tool_use"`
5. The entire response streams to the browser as SSE (`text/event-stream`) with typed events: `text`, `tool_call`, `tool_result`, `tool_error`, `done`

**Frontend** (`index.html`) is a self-contained single file served by FastAPI at `/`. It holds its own conversation history in JS memory and handles SSE streaming and markdown rendering with no build step.

## Authentication

Two separate auth contexts, both sourced from `~/.claude/.credentials.json`:

**Claude API** — `_get_claude_client()` in `app.py`:
- Uses `ANTHROPIC_API_KEY` env var if set (standard API key, `sk-ant-api03-...`)
- Falls back to `claudeAiOauth.accessToken` from credentials — OAuth tokens require `Authorization: Bearer`, not `x-api-key`; a custom `httpx.Auth` class handles this transparently

**FullStory MCP** — `_get_token()` in `app.py`:
- Reads the token under the `mcpOAuth` key whose name contains `"fullstory"`
- Automatically refreshes via `https://auth.fullstory.com/oauth/token` when the token is within 5 minutes of expiry
- Saves the refreshed token back to the credentials file

## Dependencies

All Python packages are pre-installed in `python-embed/Lib/site-packages/`. To add a new package:

```bat
python-embed\python.exe -m pip install <package>
```

Then add it to `fullstory-chat/requirements.txt` for documentation.
