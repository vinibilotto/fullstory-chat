# /fullstory-reauth

Re-authenticate the FullStory MCP server when the app returns 502 or a blank token error.

## When to use

Run this skill when:
- The app returns `502 FullStory MCP error: Illegal header value b'Bearer '`
- Any 500/502 error mentioning FullStory or MCP
- You want to verify the token is healthy before debugging

## Step 1 — Diagnose

Check whether the stored token is actually populated:

```powershell
$creds = Get-Content "$env:USERPROFILE\.claude\.credentials.json" | ConvertFrom-Json
$creds.mcpOAuth | Get-Member -MemberType NoteProperty | ForEach-Object {
    $key = $_.Name
    $val = $creds.mcpOAuth.$key
    $exp = [DateTimeOffset]::FromUnixTimeMilliseconds($val.expiresAt).ToLocalTime()
    Write-Host "Key: $key"
    Write-Host "  expiresAt: $exp"
    Write-Host "  accessToken: $(if($val.accessToken){'present ('+$val.accessToken.Length+' chars)'}else{'EMPTY'})"
    Write-Host "  refreshToken: $(if($val.refreshToken){'present'}else{'EMPTY'})"
}
```

- If `accessToken` and `refreshToken` are **EMPTY** → proceed to Step 2 (full re-auth)
- If token is present but expired → the app auto-refreshes; restart the server and retry
- If `expiresAt` shows 1969 → credentials were never written; proceed to Step 2

## Step 2 — Start OAuth flow

Call `mcp__plugin_fullstory_fullstory__authenticate` (no arguments).

This returns a URL like:
```
https://auth.fullstory.com/oauth/authorize?...&redirect_uri=http://localhost:<PORT>/callback&...
```

Give the user that URL and ask them to open it in the browser.

## Step 3 — Capture the callback URL

After the user authorizes, the browser will try to redirect to `http://localhost:<PORT>/callback?code=...` and show a **connection error** (that's expected — the port isn't a real server).

Ask the user to copy the full URL from the browser address bar and paste it here. It will look like:
```
http://localhost:47665/callback?code=XXXX&state=YYYY
```

> **Important:** complete this step quickly — the OAuth code expires in ~60 seconds. If the user is slow, start a new flow from Step 2 rather than calling `complete_authentication` with a stale code.

## Step 4 — Complete the flow

Call `mcp__plugin_fullstory_fullstory__complete_authentication` with `callback_url` set to the URL the user pasted.

On success the FullStory MCP tools (`build_segment`, `compute_metric`, etc.) will appear in the deferred tools list.

## Step 5 — Verify

Test the app:

```powershell
$body = '{"messages":[{"role":"user","content":"oi"}]}'
$resp = Invoke-WebRequest -Uri "http://localhost:8000/api/chat" -Method POST -ContentType "application/json" -Body $body -TimeoutSec 30 -SkipHttpErrorCheck
Write-Host "Status: $($resp.StatusCode)"
$resp.Content | Select-String "data:" | Select-Object -First 3
```

A `200` with `"type":"text"` lines confirms everything is working.

## Notes

- The app refreshes FullStory tokens automatically 5 minutes before expiry (`_get_token()` in `app.py`). Re-auth should only be needed when both `accessToken` and `refreshToken` are blank (e.g. after a fresh Claude Code install or credentials wipe).
- The app also auto-refreshes the Claude OAuth token (`_get_claude_token()`). If you see `400 Claude auth error`, the same proactive-refresh logic applies but the re-auth flow is different (Claude Code handles it internally).
- Error messages were made explicit: FullStory failures return `502 FullStory MCP error: <detail>`, Claude failures return `400 Claude auth error: <detail>`.
