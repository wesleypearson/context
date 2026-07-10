# Context MCP server

`@context` exposes itself as an **MCP server** so you can use it from MCP clients — Claude Code, Codex, the Claude and ChatGPT desktop apps, Cursor, and (once deployed) the web clients.

It's **owner-only** and on by default — [`app/main.py`](../app/main.py) mounts it at `/mcp`. It runs the agent as *you*, so never expose it without auth.

## The tool

`use_context(message, session_id?)` runs the *real* `context` agent ([`app/mcp.py`](../app/mcp.py)) as the owner — your full read/write/act surface behind one call. The agent decides what to do, so the same tool covers:

- **look things up** — "what's waiting on me?", "what do we know about Acme?", "what's on my calendar this week?"
- **save / update** — "met Sarah from Acme, follow up Friday", "we decided to ship MCP first"
- **act** — "draft a reply to Sarah", "tell the team the deck is ready"

One tool, not several: the client gets one obvious door for anything about your work, instead of a read-vs-write routing decision. `tools/list` returns exactly `["use_context"]` (input schema: `message` required, `session_id` optional — pass a stable `session_id` to continue a thread). This holds with MCP OAuth on too — the auth layer wraps the same curated server, it doesn't widen it.

## Before you start

Bring `@context` up locally and confirm the endpoint is live:

```sh
docker compose up -d

curl -sS --max-time 10 -o /dev/null -w '%{http_code}\n' \
  -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
# 200 = up. (400 means the Host header isn't on the allowlist — see "How it's secured".)
```

Local dev runs without auth (`MCP_CONNECT_SECRET` unset, no JWT), so any client on this machine that reaches `http://localhost:8000/mcp` is treated as **you, the owner** — the keyless-local-as-owner shortcut the rest of dev uses ([`app/mcp.py`](../app/mcp.py), `_resolve_caller_id`). That's the point locally, and the reason you don't expose a dev instance: anything on your machine that can reach the port gets your surface.

## Quick connect (one command, local dev)

From the repo root, with the stack up:

```sh
python scripts/connect.py            # add @context (local) to every MCP client found
python scripts/connect.py --dry-run  # preview, write nothing
python scripts/connect.py --remove   # undo
```

It detects Claude Code, Codex, the Claude Desktop app, and Cursor and wires the **local dev server** into each — running `claude mcp add` / `codex mcp add` for the CLIs, writing an `mcp-remote` bridge into `claude_desktop_config.json` for the desktop app, and a native `{url}` entry into `~/.cursor/mcp.json` for Cursor (absolute `npx` path resolved where a bridge is used, existing keys preserved, a timestamped backup made, anything already configured skipped). For Claude Code it also **always-allows** the `use_context` tool (adds `mcp__context__use_context` to `permissions.allow` in `~/.claude/settings.json`) so the agent never prompts you before calling it — see [Claude Code (CLI)](#claude-code-cli) below. Pure stdlib, so no venv needed. Useful flags: `--clients claude-code codex claude-desktop cursor` to limit the set, `--url` for a non-default endpoint, `--config-path` to point at a non-standard desktop config.

A **deployed** instance doesn't need this script at all — production connections ride **MCP OAuth**: clients connect by URL and you approve each one on a consent page. See [Production: MCP OAuth](#production-mcp-oauth) below; the [README](../README.md#connect-production-context-mcp-server) has the quick-start.

The per-client sections below are what it automates — reach for them to do it by hand, or to understand exactly what each form writes.

## Claude Code (CLI)

```sh
claude mcp add -s user --transport http context http://localhost:8000/mcp
claude mcp list      # context: http://localhost:8000/mcp (HTTP) - ✓ Connected
```

**Scope: `user`.** @context is a personal, machine-wide endpoint you want in *every* project, so register it at user scope (`-s user`). The default `local` scope would limit it to the current directory; `project` scope writes a shared `.mcp.json` into the repo, which would push a localhost-only, owner-bound connector onto everyone who clones it — wrong for a personal endpoint. The client then picks up `use_context` and uses it on its own; you rarely have to name @context.

**Always-allow the tool.** `claude mcp add` registers the server but doesn't grant it, so Claude Code prompts you on every call. `scripts/connect.py` adds the permission for you; to do it by hand, drop the tool's rule into `permissions.allow` in `~/.claude/settings.json` (user scope, to match the server registration):

```jsonc
{ "permissions": { "allow": ["mcp__context__use_context"] } }
```

This only governs Claude Code's local prompt — the deployed server stays OAuth + owner-gated and fail-closed (see [`docs/SECURITY.md`](SECURITY.md) L7), so allow-listing the tool here doesn't widen the production boundary. `python scripts/connect.py --remove` takes the rule back out.

For a deployed instance, register the URL and sign in over OAuth:

```sh
claude mcp add --transport http context https://<your-domain>/mcp
claude mcp login context     # opens the browser consent — approve with MCP_CONNECT_SECRET
```

## Codex (CLI)

```sh
codex mcp add --url http://localhost:8000/mcp context
codex mcp get context     # transport: streamable_http
```

`--url` registers a streamable-HTTP server — Codex writes it to `~/.codex/config.toml` as `[mcp_servers.context]`. No experimental flags are needed for an unauthenticated local server. For a deployed instance, register the production URL and log in:

```sh
codex mcp add --url https://<your-domain>/mcp context
codex mcp login context      # browser consent, approved with MCP_CONNECT_SECRET
```

## Claude Desktop

Claude Desktop runs on your machine, so it *can* reach `http://localhost:8000/mcp` — just not through its connector UI (that only accepts `https` URLs, so it's the wrong door *for local dev*; it's the right door for a deployed instance — see below). Use the config file for local. `scripts/connect.py` writes this for you; to do it by hand:

**Local: config-file + stdio bridge.** Claude Desktop's config-file MCP support is stdio-only, so bridge the HTTP endpoint with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote). Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "context": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8000/mcp", "--transport", "http-only"]
    }
  }
}
```

Restart the app and `use_context` shows up under the app's tools. (Verified: `mcp-remote` connects to the local server over StreamableHTTP and proxies it to the app over stdio.)

> **`PATH` gotcha.** GUI apps on macOS don't inherit your shell `PATH`, so the app may fail to launch a bare `npx`. If the server doesn't connect, set `"command"` to the absolute path — find it with `which npx` (e.g. `/opt/homebrew/bin/npx` for a Homebrew Node). On Windows, Claude Desktop can't exec `npx.cmd` directly — use `"command": "cmd"` with `"args": ["/c", "npx", "-y", "mcp-remote", …]`. `scripts/connect.py` writes the right form for your OS automatically (the Windows path is best-effort — untested by us, since we develop on macOS).

**Deployed: the connector UI is the recommended door.** **Settings → Connectors → Add custom connector** speaks OAuth — exactly what a deployed @context serves once `MCP_CONNECT_SECRET` is set. Add `https://<your-domain>/mcp`, leave the OAuth Client ID / Secret fields **empty** (the server registers the client dynamically), click Connect, and approve the consent page with the secret. If you'd rather keep the config-file route, the bridge works there too — swap the URL for `https://<your-domain>/mcp` and `mcp-remote` drives the same OAuth browser flow (no headers to configure; there are no static bearer tokens anymore).

## Cursor

Cursor speaks remote MCP **natively** — no `mcp-remote` bridge, no npx. `scripts/connect.py` writes the local entry for you (it's one of the default clients); to do it by hand, add a `{url}` entry to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "context": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Restart Cursor and `use_context` shows up under its MCP tools. For a **deployed** instance, use the same entry with `https://<your-domain>/mcp` — no `headers` block: Cursor's **Connect** button triggers the OAuth flow, and you approve the consent page with `MCP_CONNECT_SECRET`.

## ChatGPT desktop

ChatGPT desktop has **no local MCP config** — there's no `claude_desktop_config.json` equivalent to write a stdio bridge into (verified on macOS by inspecting the app's support dir: only connectors/"Work with Apps" pairings, no `mcpServers`). It reaches MCP servers only as a **remote HTTPS connector**, so the one way to use @context from ChatGPT is a deployed or tunnelled HTTPS instance — the next section.

## ChatGPT web / Claude web / cloud (deploy or tunnel)

Cloud clients — ChatGPT on the web, Claude on the web — run on a remote server and **cannot reach your laptop**. They need a public HTTPS URL. Two ways, the same two we use for Slack.

**Deploy it (recommended).** Deploy `@context` (the Railway steps in the [README](../README.md)) and you get a public domain; the endpoint is `https://<your-domain>/mcp`. With `MCP_CONNECT_SECRET` set (the deploy scripts generate it), the server is its own OAuth authorization server, and the connector UIs walk straight through it:

1. Add `https://<your-domain>/mcp` as a **custom connector** (claude.ai: Settings → Connectors; ChatGPT: Settings → Connectors).
2. **Leave the OAuth Client ID / Secret fields empty** — the server supports dynamic client registration.
3. Click Connect. A consent page opens (`/mcp-auth/consent`); type your `MCP_CONNECT_SECRET` to approve.

Make sure `AGENTOS_URL` is your exact public origin — it's both the OAuth server's advertised origin and the Host allowlist anchor (see below).

**Tunnel for a quick test (ngrok).**

```sh
ngrok http 8000
# Set AGENTOS_URL to the tunnel domain so the server accepts that Host,
# then use https://<id>.ngrok.app/mcp
```

> ⚠️ A tunnel to a **dev** instance has no auth, so the owner gate falls back to "you" for *anyone who has the URL* — an open door to your context. Only tunnel a production-configured run (`RUNTIME_ENV=prd` + `MCP_CONNECT_SECRET`), or keep it ephemeral and shut it down right after.

## Production: MCP OAuth

The deployed `/mcp` endpoint is gated by **MCP OAuth** — the deployment is its own OAuth 2.1 authorization server. No tokens to mint, nothing to paste into client configs; a client connects by URL and you approve it once.

**Arm it.** Set `MCP_CONNECT_SECRET` (≥16 chars — `openssl rand -base64 32`) and [`app/main.py`](../app/main.py) builds `AgentOSBuiltinAuth(url=AGENTOS_URL, secret=…, signing_key_material=AGENTOS_MCP_SIGNING_KEY)` and hands it to `AgentOS(mcp_auth=…)`. That turns on, all inside your deployment:

- **Dynamic client registration + PKCE** — connector UIs and CLI logins register themselves; you never create OAuth clients by hand (leave the client id/secret fields empty).
- **A consent page** at `/mcp-auth/consent`, gated on `MCP_CONNECT_SECRET`. On a single-owner deploy the secret *is* an owner credential — only someone who has it can approve a client, so treat it like a password.
- **Token issuance** — 1h HS256 access tokens, 30d rotating refresh tokens. OAuth state lives hashed at rest in your existing Postgres (five auto-created tables); nothing leaves your infrastructure.
- **Discovery** — an unauthenticated request gets the RFC 9728 challenge, which is what lets connector UIs and `claude mcp login` find the authorization server on their own.

`AGENTOS_URL` must be the **exact public origin** — HTTPS is enforced for non-localhost at construction, so a plaintext non-localhost URL fails boot (localhost is fine for dev).

**The signing key.** `AGENTOS_MCP_SIGNING_KEY` (≥32 chars) optionally pins the HS256 signing root in env; unset, a key is generated and persisted in Postgres. Pin it in production. Rotation semantics are the control surface:

- Rotate `AGENTOS_MCP_SIGNING_KEY` → **every issued token is revoked** (the kill switch); all clients re-consent.
- Rotate `MCP_CONNECT_SECRET` → only future consents are gated on the new secret; existing tokens keep working.

**One command.** [`scripts/setup_context.sh`](../scripts/setup_context.sh) chains the whole thing: preflight (`railway login`, project link) → ensure `MCP_CONNECT_SECRET` + `AGENTOS_MCP_SIGNING_KEY` exist in `.env.production` (generated when missing) → `./scripts/railway/env-sync.sh` → `railway up` redeploy (`--no-redeploy` skips it) → print the connector recipe with your real domain and secret. `scripts/railway/up.sh` already does the secret generation on first provisioning.

**Connect the clients.**

| Client | How |
|---|---|
| claude.ai / ChatGPT (web) | Add `https://<your-domain>/mcp` as a custom connector, leave the OAuth client id/secret fields empty, Connect → consent page → type the secret. |
| Claude Code | `claude mcp add --transport http context https://<your-domain>/mcp` then `claude mcp login context` (browser consent). |
| Codex | `codex mcp add --url https://<your-domain>/mcp context` then `codex mcp login context`. |
| Cursor | Native `{url}` entry in `~/.cursor/mcp.json`; the Connect button triggers OAuth. |
| Claude Desktop | The connector UI (recommended), or the `mcp-remote` bridge — it drives the OAuth browser flow itself. |
| All CLIs at once | `uvx agno connect --url https://<your-domain>` wires each client found and prints its sign-in step. |

The **AgentOS UI** (os.agno.com) is unaffected — it authenticates against the REST API with the control-plane JWT (`JWT_VERIFICATION_KEY`), a separate door from MCP OAuth. And the **single-replica constraint still applies**: OAuth gates the door, but MCP transport sessions are still held in-process, so `/mcp` needs `numReplicas: 1` (see [`docs/SCALING.md`](SCALING.md)).

## How it's secured

- **Token verification first.** In production every `/mcp` request is verified by agno before anything else runs: MCP OAuth access tokens (when armed), service-account PATs, and control-plane JWTs all resolve through the same verification layer, and an identity bridge stamps the verified principal as the request's `user_id`. An unauthenticated request gets the RFC 9728 challenge (that's discovery, not a bypass). Nothing @context does can see an unverified identity.
- **Owner-only, in code.** After verification, the `authorize` gate (`MCPServerConfig.authorize=_caller_is_owner`, [`app/mcp.py`](../app/mcp.py)) 401s anyone who isn't a configured `OWNER_ID` identity — with one addition: while OAuth is armed, the reserved `__oauth__:<client_id>` principal is trusted, because it's minted only by *this deployment's own* consent flow (approving a client requires typing `MCP_CONNECT_SECRET`, an owner credential on a single-owner deploy) and agno rejects any external token that claims the reserved namespace. `sa:` service-account principals get **no** surface — a PAT verifies but the owner gate 401s it. The gate never falls back to the guest surface, and with the secret unset the `__oauth__:` prefix is rejected like everything else. The deterministic eval `mcp_server_is_owner_only` proves both directions: `__oauth__:` accepted iff OAuth is armed, `sa:` always rejected. (Details: [`SECURITY.md`](SECURITY.md) L7.)
- **DNS-rebinding protection** is on (`MCPServerConfig.allowed_hosts`), because an always-on local server is exactly what it protects. The Host allowlist is anchored on localhost (so the desktop/CLI case needs no config) plus the host from `AGENTOS_URL` (so a deploy or tunnel works — point `AGENTOS_URL` at that domain). A request with any other Host is rejected with **400** (verified locally).
- **The curated surface survives auth.** OAuth wraps the same one-tool server — `tools/list` still returns exactly `["use_context"]`, none of AgentOS's built-in MCP tools are exposed, and `use_context` still pins its runs to the canonical owner id.
- **Acting.** Reads, drafting email, Slack messages, and filing all run to completion. The one approval-gated act tool — `update_calendar` — still pauses for approval, and there's no approval affordance over MCP, so the tool returns a note telling you to approve it in the AgentOS chat UI and ask it to continue.

## Verifying it runs as the owner

With the stack up, `./scripts/mcp_check.sh` proves the endpoint end to end: handshake, `tools/list` returning **exactly** `["use_context"]` (more tools would mean the curated server regressed to the generic surface), then one `use_context` call. On an auth-gated instance it instead proves the gate: a probe service account authenticates but is 401'd by the owner gate — owner-only, demonstrated.

Or by hand: point a streamable-HTTP MCP client at `http://localhost:8000/mcp` (any of the clients above, or a short script using the `mcp` Python SDK's `streamablehttp_client`). `tools/list` returns `["use_context"]`; calling it with a workspace question — *"what is the MCP endpoint path and which file defines it?"* — comes back citing real repo files (proof the owner toolset is threaded through), and a statement to remember gets filed into your context.
