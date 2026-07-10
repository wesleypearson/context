#!/usr/bin/env python3
"""
@context MCP connector — add the local dev @context to your MCP clients in one command.

@context's MCP server (`use_context`) is one command to add to a CLI client, but
the desktop apps need a hand-edited config + an `mcp-remote` stdio bridge. This
script does all of it for you, safely:

  - Claude Code  — runs `claude mcp add -s user --transport http <name> <url>`,
                   then always-allows the `use_context` tool in ~/.claude/settings.json
                   (adds `mcp__<name>__use_context` to permissions.allow) so it never prompts
  - Codex        — runs `codex mcp add --url <url> <name>`
  - Claude Desktop — merges an `mcp-remote` bridge into claude_desktop_config.json
                     (absolute npx path, existing keys preserved, timestamped backup)
  - Cursor       — merges a native remote entry ({url}) into ~/.cursor/mcp.json
                   (Cursor speaks streamable HTTP directly, so no mcp-remote bridge;
                   existing keys preserved, timestamped backup)

It only touches clients it finds, skips anything already wired up (idempotent),
and never sends or reads anything — it just writes local client config.

Local dev only. A **deployed** @context uses MCP OAuth instead of local config
surgery: set `MCP_CONNECT_SECRET`, then connect clients by URL (claude.ai /
ChatGPT: add `https://<your-domain>/mcp` as a custom connector and approve the
consent page with the secret; CLI clients: `uvx agno connect --url
https://<your-domain>`). See docs/MCP.md.

Not auto-configured: ChatGPT (desktop + web) has no local MCP config file — it
only reaches MCP servers as a remote HTTPS connector, so it needs a deployed /
tunnelled instance (see docs/MCP.md), not a localhost bridge. Windows is
best-effort: the config path is handled and the bridge is written in the
`cmd /c npx` form Claude Desktop expects there, but we develop on macOS and
haven't tested the Windows path end to end.

Usage (no venv needed — pure stdlib):

    python scripts/connect.py                      # add the local dev server to every client found
    python scripts/connect.py --dry-run            # show what would change, write nothing
    python scripts/connect.py --remove             # undo (remove the entries)
    python scripts/connect.py --clients claude-desktop
    python scripts/connect.py --url http://localhost:8000/mcp --name context
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_URL = "http://localhost:8000/mcp"
DEFAULT_NAME = "context"
SERVER_TOOL = "use_context"  # the single tool the server exposes; auto-allowed in Claude Code so it never prompts
CLIENTS = ("claude-code", "codex", "claude-desktop", "cursor")

# Status glyphs for the summary line.
OK, SKIP, FAIL = "✓", "–", "✗"


def claude_desktop_config_path() -> Path:
    """Default Claude Desktop config path for this OS."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        import os

        return Path(os.environ.get("APPDATA", home)) / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def cursor_config_path() -> Path:
    """Cursor's global MCP config path (same layout on every OS: ~/.cursor/mcp.json)."""
    return Path.home() / ".cursor" / "mcp.json"


def claude_code_settings_path() -> Path:
    """Claude Code's user-scope settings file — where the always-allow rule lives.

    The MCP server is registered with `claude mcp add -s user`, so the matching
    permission belongs at user scope too (applies wherever you connect, not just
    this project — and stays out of any committed repo settings).
    """
    return Path.home() / ".claude" / "settings.json"


def bridge_entry(url: str, npx: str) -> dict:
    """The claude_desktop_config.json server entry — an mcp-remote stdio bridge.

    Claude Desktop's config-file MCP support is stdio-only, so mcp-remote
    bridges the streamable-HTTP endpoint. `http-only` matches our server (it
    speaks streamable HTTP, not SSE). Local dev has no auth, so no headers.

    Platform note: macOS/Linux GUI apps don't inherit the shell PATH, so we
    pin the absolute `npx`. Windows installs Node on the system PATH (which GUI
    apps do inherit) but Claude Desktop can't exec `npx.cmd` directly, so the
    documented form there is `cmd /c npx ...`. The Windows path is best-effort —
    untested by us (we develop on macOS); verify it connects after a restart.
    """
    args = ["-y", "mcp-remote", url, "--transport", "http-only"]
    if sys.platform.startswith("win"):
        return {"command": "cmd", "args": ["/c", "npx", *args]}
    return {"command": npx, "args": args}


# ---------------------------------------------------------------------------
# Claude Desktop (config-file bridge)
# ---------------------------------------------------------------------------
def connect_claude_desktop(url: str, name: str, config_path: Path, *, remove: bool, dry_run: bool) -> tuple[str, str]:
    if not remove and not config_path.parent.exists():
        return SKIP, "claude-desktop: app not found (no config dir) — skipped"

    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            return FAIL, f"claude-desktop: {config_path} is not valid JSON ({exc}) — left untouched"

    servers = config.get("mcpServers", {})

    if remove:
        if name not in servers:
            return SKIP, f"claude-desktop: no '{name}' entry to remove"
        if dry_run:
            return OK, f"claude-desktop: would remove '{name}' from {config_path}"
        _backup(config_path)
        servers.pop(name, None)
        config["mcpServers"] = servers
        _write_json(config_path, config)
        return OK, f"claude-desktop: removed '{name}' (restart Claude Desktop)"

    npx = shutil.which("npx")
    if not npx:
        return FAIL, "claude-desktop: `npx` not found on PATH — install Node, then re-run"

    entry = bridge_entry(url, npx)
    if servers.get(name) == entry:
        return SKIP, f"claude-desktop: '{name}' already configured ({config_path})"

    if dry_run:
        action = "update" if name in servers else "add"
        return OK, f"claude-desktop: would {action} '{name}' in {config_path}\n      {json.dumps(entry)}"

    _backup(config_path)
    servers[name] = entry
    config["mcpServers"] = servers
    _write_json(config_path, config)
    return OK, f"claude-desktop: wrote '{name}' to {config_path} (restart Claude Desktop)"


def _backup(path: Path) -> None:
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(path.read_text())


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Cursor (config-file, native remote entry)
# ---------------------------------------------------------------------------
def cursor_entry(url: str) -> dict:
    """The ~/.cursor/mcp.json server entry — a native streamable-HTTP remote.

    Unlike Claude Desktop, Cursor speaks remote MCP directly: a `{url}` object,
    no `mcp-remote` stdio bridge (and so no npx dependency). Local dev has no
    auth, so the entry is just the URL.
    """
    return {"url": url}


def connect_cursor(url: str, name: str, config_path: Path, *, remove: bool, dry_run: bool) -> tuple[str, str]:
    if not remove and not config_path.parent.exists():
        return SKIP, "cursor: app not found (no ~/.cursor dir) — skipped"

    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            return FAIL, f"cursor: {config_path} is not valid JSON ({exc}) — left untouched"

    servers = config.get("mcpServers", {})

    if remove:
        if name not in servers:
            return SKIP, f"cursor: no '{name}' entry to remove"
        if dry_run:
            return OK, f"cursor: would remove '{name}' from {config_path}"
        _backup(config_path)
        servers.pop(name, None)
        config["mcpServers"] = servers
        _write_json(config_path, config)
        return OK, f"cursor: removed '{name}' (restart Cursor)"

    entry = cursor_entry(url)
    if servers.get(name) == entry:
        return SKIP, f"cursor: '{name}' already configured ({config_path})"

    if dry_run:
        action = "update" if name in servers else "add"
        return OK, f"cursor: would {action} '{name}' in {config_path}\n      {json.dumps(entry)}"

    _backup(config_path)
    servers[name] = entry
    config["mcpServers"] = servers
    _write_json(config_path, config)
    return OK, f"cursor: wrote '{name}' to {config_path} (restart Cursor)"


# ---------------------------------------------------------------------------
# CLI clients (Claude Code, Codex)
# ---------------------------------------------------------------------------
def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def connect_cli(client: str, url: str, name: str, *, remove: bool, dry_run: bool, force: bool) -> tuple[str, str]:
    """Register/unregister with a CLI client whose binary is on PATH."""
    binary = "claude" if client == "claude-code" else "codex"
    if not shutil.which(binary):
        return SKIP, f"{client}: `{binary}` not found on PATH — skipped"

    get_cmd = [binary, "mcp", "get", name]
    if client == "claude-code":
        add_cmd = [binary, "mcp", "add", "-s", "user", "--transport", "http", name, url]
    else:
        add_cmd = [binary, "mcp", "add", "--url", url, name]
    remove_cmd = [binary, "mcp", "remove", name]

    if remove:
        if dry_run:
            return OK, f"{client}: would run `{' '.join(remove_cmd)}`"
        res = _run(remove_cmd)
        if res.returncode == 0:
            return OK, f"{client}: removed '{name}'"
        return SKIP, f"{client}: nothing to remove ({_first_line(res.stderr) or 'not configured'})"

    exists = _run(get_cmd).returncode == 0
    if exists and not force:
        return SKIP, f"{client}: '{name}' already connected (use --force to re-add)"

    if dry_run:
        return OK, f"{client}: would run `{' '.join(add_cmd)}`"

    if exists and force:
        _run(remove_cmd)
    res = _run(add_cmd)
    if res.returncode == 0:
        return OK, f"{client}: added '{name}' → {url}"
    return FAIL, f"{client}: `{binary} mcp add` failed — {_first_line(res.stderr) or 'see output'}"


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def allow_claude_code_tool(name: str, *, remove: bool, dry_run: bool) -> tuple[str, str]:
    """Always-allow the server's `use_context` tool in Claude Code's user settings.

    `claude mcp add` registers the server but doesn't grant it — without this,
    Claude Code prompts the owner on every call. We add the precise tool rule
    `mcp__<name>__use_context` to `permissions.allow` in ~/.claude/settings.json
    so it runs freely. Idempotent, preserves every other setting, and only ever
    touches `permissions.allow`. `--remove` takes the rule back out.

    (This governs only Claude Code's local prompt; the deployed server stays
    OAuth + owner-gated and fail-closed, so the production boundary is untouched.)
    """
    rule = f"mcp__{name}__{SERVER_TOOL}"
    path = claude_code_settings_path()

    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            return FAIL, f"claude-code: {path} is not valid JSON ({exc}) — allow rule left untouched"

    perms = settings.get("permissions") or {}
    allow = perms.get("allow") or []

    if remove:
        if rule not in allow:
            return SKIP, f"claude-code: no '{rule}' allow rule to remove"
        if dry_run:
            return OK, f"claude-code: would remove allow rule '{rule}' from {path}"
        perms["allow"] = [r for r in allow if r != rule]
        settings["permissions"] = perms
        _write_json(path, settings)
        return OK, f"claude-code: removed allow rule '{rule}'"

    if rule in allow:
        return SKIP, f"claude-code: '{rule}' already always-allowed ({path})"
    if dry_run:
        return OK, f"claude-code: would always-allow '{rule}' in {path}"
    allow.append(rule)
    perms["allow"] = allow
    settings["permissions"] = perms
    _write_json(path, settings)
    return OK, f"claude-code: always-allowed '{rule}' (no more prompts)"


# ---------------------------------------------------------------------------
# Server reachability (soft check)
# ---------------------------------------------------------------------------
def server_reachable(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add @context's local dev MCP server to your MCP clients. "
        "(Production uses OAuth — set MCP_CONNECT_SECRET and connect clients by URL, "
        "or run `uvx agno connect`; see docs/MCP.md.)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint (default: {DEFAULT_URL})")
    parser.add_argument("--name", default=DEFAULT_NAME, help=f"server name in each client (default: {DEFAULT_NAME})")
    parser.add_argument(
        "--clients", nargs="+", choices=CLIENTS, default=list(CLIENTS), help="limit to specific clients"
    )
    parser.add_argument("--remove", action="store_true", help="remove @context from the clients instead of adding")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument("--force", action="store_true", help="re-add to CLI clients even if already present")
    parser.add_argument(
        "--config-path", type=Path, default=None, help="override the Claude Desktop config path (advanced/testing)"
    )
    args = parser.parse_args()

    url = args.url
    verb = "Removing" if args.remove else "Adding"
    print(f"{verb} @context ('{args.name}' → {url}) [local]{' [dry-run]' if args.dry_run else ''}\n")

    if not args.remove and not args.dry_run and not server_reachable(url):
        print(f"  note: nothing is listening at {url} yet — start it with `docker compose up -d`.")
        print("        (writing client config anyway; it'll connect once the server is up.)\n")

    desktop_config = args.config_path or claude_desktop_config_path()
    results: list[tuple[str, str]] = []
    for client in args.clients:
        if client == "claude-desktop":
            results.append(
                connect_claude_desktop(url, args.name, desktop_config, remove=args.remove, dry_run=args.dry_run)
            )
        elif client == "cursor":
            results.append(
                connect_cursor(url, args.name, cursor_config_path(), remove=args.remove, dry_run=args.dry_run)
            )
        else:
            results.append(
                connect_cli(client, url, args.name, remove=args.remove, dry_run=args.dry_run, force=args.force)
            )

    # Claude Code prompts before each MCP call until the tool is allow-listed, so once the
    # server is registered, always-allow its `use_context` tool too (only when Claude Code is
    # actually present — mirrors connect_cli's own skip).
    if "claude-code" in args.clients and shutil.which("claude"):
        results.append(allow_claude_code_tool(args.name, remove=args.remove, dry_run=args.dry_run))

    for glyph, message in results:
        print(f"  {glyph} {message}")

    if not args.remove:
        print(
            "\n  ChatGPT (desktop + web): no local config to write — it only reaches MCP servers as a\n"
            "  remote HTTPS connector. Deploy or tunnel, then add https://<domain>/mcp as a custom\n"
            "  connector (see docs/MCP.md)."
        )
        print(
            "\n  Deployed @context? Production uses OAuth — set MCP_CONNECT_SECRET and connect clients\n"
            "  by URL (or `uvx agno connect --url https://<your-domain>`). See docs/MCP.md."
        )
        if sys.platform.startswith("win") and "claude-desktop" in args.clients:
            print(
                "\n  Windows: the Claude Desktop bridge is best-effort/untested — after restarting the\n"
                "  app, confirm `context` connects; the npx invocation may need adjusting."
            )

    if not args.remove and not args.dry_run and any(g == OK for g, _ in results):
        print(
            "\nDone. Restart the desktop apps you configured (Claude Desktop, Cursor) to pick up the new"
            " config; CLI clients (Claude Code, Codex) pick it up on their next run."
        )
    return 1 if any(glyph == FAIL for glyph, _ in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
