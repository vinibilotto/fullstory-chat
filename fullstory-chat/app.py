import asyncio
import json
import os
import time
from pathlib import Path

import anthropic
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MCP_URL = "https://api.fullstory.com/mcp/fullstory"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

SYSTEM_PROMPT = """You are a FullStory analytics assistant with direct access to a FullStory account via MCP tools.

You help with:
- Analyzing user behavior (counts, rates, trends, breakdowns)
- Creating and computing segments and metrics
- Investigating session replays to identify UX problems
- Identifying UX improvement opportunities

Guidelines:
- Always search for existing metrics/segments before creating new ones
- Default time range is last_30_days unless the user specifies otherwise
- Always surface metric_url so the user can verify results in FullStory
- When results are zero or anomalous, investigate before concluding
- Be concise and focus on actionable insights"""


# ── Credentials & token management ────────────────────────────────────────────

def _load_creds() -> dict:
    with open(CREDENTIALS_PATH) as f:
        return json.load(f)


def _save_creds(creds: dict):
    with open(CREDENTIALS_PATH, "w") as f:
        json.dump(creds, f, indent=2)


_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _find_fs_entry(creds: dict) -> tuple[str, dict]:
    for key, val in creds.get("mcpOAuth", {}).items():
        if "fullstory" in key.lower():
            return key, val
    raise ValueError("FullStory OAuth token not found in ~/.claude/.credentials.json")


def _oauth_refresh(url: str, refresh_token: str, client_id: str) -> dict:
    import urllib.request, urllib.parse
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _get_token() -> str:
    creds = _load_creds()
    key, entry = _find_fs_entry(creds)
    now = time.time()
    expires_at = entry.get("expiresAt", 0) / 1000
    # Refresh proactively 5 minutes before expiry
    if expires_at > now + 300:
        return entry["accessToken"]
    try:
        new = _oauth_refresh(
            "https://auth.fullstory.com/oauth/token",
            entry["refreshToken"],
            entry["clientId"],
        )
        entry["accessToken"] = new["access_token"]
        entry["expiresAt"] = int((now + new.get("expires_in", 3600)) * 1000)
        if "refresh_token" in new:
            entry["refreshToken"] = new["refresh_token"]
        creds["mcpOAuth"][key] = entry
        _save_creds(creds)
        return entry["accessToken"]
    except Exception:
        # Token may still work; let the API call fail with a proper error if not
        return entry["accessToken"]


def _get_claude_token() -> str:
    creds = _load_creds()
    entry = creds.get("claudeAiOauth", {})
    now = time.time()
    expires_at = entry.get("expiresAt", 0) / 1000
    # Refresh proactively 5 minutes before expiry
    if expires_at > now + 300:
        return entry["accessToken"]
    try:
        new = _oauth_refresh(
            "https://api.anthropic.com/v1/oauth/token",
            entry["refreshToken"],
            _CLAUDE_CLIENT_ID,
        )
        entry["accessToken"] = new["access_token"]
        entry["expiresAt"] = int((now + new.get("expires_in", 28800)) * 1000)
        if "refresh_token" in new:
            entry["refreshToken"] = new["refresh_token"]
        creds["claudeAiOauth"] = entry
        _save_creds(creds)
        return entry["accessToken"]
    except Exception:
        # Token may still work; let the API call fail with a proper error if not
        return entry["accessToken"]


# ── MCP helpers ────────────────────────────────────────────────────────────────

async def _mcp(method: str, params: dict | None = None) -> dict:
    token = _get_token()
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        body["params"] = params
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            MCP_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
    if "error" in data:
        raise RuntimeError(f"MCP error {data['error'].get('code')}: {data['error'].get('message')}")
    return data.get("result", {})


async def get_tools() -> list[dict]:
    result = await _mcp("tools/list")
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        }
        for t in result.get("tools", [])
    ]


async def call_tool(name: str, arguments: dict) -> str:
    result = await _mcp("tools/call", {"name": name, "arguments": arguments})
    content = result.get("content", [])
    if isinstance(content, list):
        return "\n".join(
            item.get("text", json.dumps(item))
            for item in content
            if isinstance(item, dict)
        )
    return json.dumps(result)


# ── Chat endpoint ──────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages: list[dict]


def _get_claude_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    token = _get_claude_token()
    class _OAuthAuth(httpx.Auth):
        def auth_flow(self, request):
            request.headers["Authorization"] = f"Bearer {token}"
            request.headers.pop("x-api-key", None)
            yield request
    return anthropic.Anthropic(api_key="__oauth__", http_client=httpx.Client(auth=_OAuthAuth()))


@app.post("/api/chat")
async def chat(req: ChatRequest):
    try:
        client = _get_claude_client()
    except Exception as e:
        raise HTTPException(400, f"Claude auth error: {e}")

    try:
        tools = await get_tools()
    except Exception as e:
        raise HTTPException(502, f"FullStory MCP error: {e}")

    async def stream() -> str:
        messages = list(req.messages)
        try:
            while True:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=8096,
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=tools,
                    messages=messages,
                )

                for block in response.content:
                    if block.type == "text" and block.text:
                        yield f"data: {json.dumps({'type': 'text', 'text': block.text})}\n\n"

                if response.stop_reason != "tool_use":
                    break

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                messages.append({
                    "role": "assistant",
                    "content": [b.model_dump() for b in response.content],
                })

                tool_results = []
                for tu in tool_uses:
                    yield f"data: {json.dumps({'type': 'tool_call', 'name': tu.name})}\n\n"
                    try:
                        result_text = await call_tool(tu.name, tu.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": result_text,
                        })
                        yield f"data: {json.dumps({'type': 'tool_result', 'name': tu.name})}\n\n"
                    except Exception as exc:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": f"Error: {exc}",
                            "is_error": True,
                        })
                        yield f"data: {json.dumps({'type': 'tool_error', 'name': tu.name, 'error': str(exc)})}\n\n"

                messages.append({"role": "user", "content": tool_results})

            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── Serve frontend ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
