#!/bin/bash

############################################################################
#
#    MCP Smoke Check
#
#    Proves the @context MCP endpoint end to end. On an open (dev/keyless)
#    instance: handshake, tools/list — which must return EXACTLY
#    ["use_context"] (the curated owner-only server; more tools would mean
#    it regressed to the generic AgentOS surface) — then one use_context
#    call. Runs the client inside the container.
#
#    When /mcp is auth-gated (MCP_CONNECT_SECRET set, or prd JWT), the check
#    mints a short-lived probe service account and connects with it — and
#    expects the OWNER GATE to 401 it: @context's `authorize` rejects `sa:`
#    principals, so a 401 *after* successful token auth is the PASS (it
#    proves owner-only). The functional tools/list leg only runs on the
#    open dev instance (compose). The probe account is deleted afterwards —
#    even on failure.
#
#    Usage:
#      ./scripts/mcp_check.sh                              # quick default probe
#      ./scripts/mcp_check.sh "What's the MCP endpoint?"   # your own question
#
############################################################################

set -e

# Colors
ORANGE='\033[38;5;208m'
RED='\033[31m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

DEFAULT_QUESTION="Without using any tools, introduce yourself in two short sentences."
QUESTION="${1:-$DEFAULT_QUESTION}"

echo ""
echo -e "${ORANGE}▸${NC} ${BOLD}MCP Check${NC}"
echo ""
echo -e "${DIM}> http://localhost:8000/mcp  (client runs inside context-api)${NC}"
echo ""

if docker compose exec -T context-api python -u - "$QUESTION" <<'PY'
import asyncio
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ORANGE = "\033[38;5;208m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
NC = "\033[0m"

EXPECTED_TOOLS = ["use_context"]  # the curated surface — exactly one tool


def step(text: str) -> None:
    print(f"{ORANGE}✓{NC} {text}", flush=True)


def fail(text: str) -> None:
    print(f"{RED}✗{NC} {text}", flush=True)
    raise SystemExit(1)


async def run_check(headers: dict | None, auth_note: str) -> None:
    async with streamablehttp_client("http://localhost:8000/mcp", headers=headers, timeout=180) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            step(f"Handshake — {auth_note}")
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            if names != EXPECTED_TOOLS:
                # The whole point of this server is the curated one-tool surface.
                # Seeing anything else (e.g. the 8 generic AgentOS tools) means it
                # regressed to the generic MCP surface — fail loudly.
                fail(
                    f"tools/list returned {len(names)} tools {names} — expected exactly "
                    f"{EXPECTED_TOOLS}. The curated owner-only server has regressed to "
                    "the generic surface (check mcp_server=context_mcp_config() in app/main.py)."
                )
            step("MCP OK — 1 tool (use_context)")
            start = time.perf_counter()
            result = await session.call_tool(
                "use_context",
                {"message": sys.argv[1]},
                read_timeout_seconds=None,
            )
            elapsed = time.perf_counter() - start
            step(f"use_context — answered in {elapsed:.1f}s")
            print(flush=True)
            print(f"{DIM}Agent response:{NC}", flush=True)
            print(flush=True)
            print(result.content[0].text, flush=True)


def is_unauthorized(exc: BaseException) -> bool:
    """True when the failure chain contains an HTTP 401 (auth-gated /mcp)."""
    if isinstance(exc, BaseExceptionGroup):
        return any(is_unauthorized(e) for e in exc.exceptions)
    return "401" in str(exc) or any(is_unauthorized(e) for e in (exc.__cause__, exc.__context__) if e)


async def run_gated_check_with_probe_pat() -> None:
    """Mint a probe service account, connect with it, and expect the owner gate to 401 it.

    The token authenticates (agno's MultiAuth accepts service-account PATs), but
    @context's `authorize` gate rejects `sa:` principals — so a 401 here is the
    PASS: it proves the endpoint is owner-only, not merely auth-gated. The probe
    getting through would mean a non-owner reached the owner surface — fail loudly.
    Always deletes the account, even on failure.
    """
    import time as _time
    import uuid

    from agno.db.schemas.service_accounts import ServiceAccount
    from agno.os.service_accounts import DEFAULT_SERVICE_ACCOUNT_SCOPES, generate_token
    from db import get_postgres_db

    db = get_postgres_db()
    plaintext, token_hash, token_prefix = generate_token()
    now = int(_time.time())
    account = ServiceAccount(
        id=str(uuid.uuid4()),
        name=f"mcp-check-probe-{now}",
        token_hash=token_hash,
        token_prefix=token_prefix,
        scopes=list(DEFAULT_SERVICE_ACCOUNT_SCOPES),
        created_at=now,
        expires_at=now + 600,
        created_by="mcp_check.sh",
        user_id=None,
    )
    db.create_service_account(account.to_dict())
    try:
        await run_check(
            {"Authorization": f"Bearer {plaintext}"},
            "auth-gated; connected with a short-lived probe token",
        )
    except BaseException as exc:
        if is_unauthorized(exc):
            step("Owner gate — 401'd the authenticated probe (sa: principal): owner-only proven")
            print(f"{DIM}Functional tools/list + use_context legs run on the open dev instance (compose).{NC}", flush=True)
            return
        raise
    finally:
        db.delete_service_account(account.id)
    # The probe reached the owner surface — that's a boundary breach, not a pass.
    fail(
        "An authenticated service-account probe (sa: principal) got through the owner "
        "gate to the owner surface. _caller_is_owner in app/mcp.py must reject sa: — "
        "the MCP server is owner-only."
    )


async def main() -> None:
    try:
        await run_check(None, "open (dev)")
    except SystemExit:
        raise
    except BaseException as exc:  # ExceptionGroup from the transport on 401
        if not is_unauthorized(exc):
            raise
        await run_gated_check_with_probe_pat()


asyncio.run(main())
PY
then
    echo ""
    echo -e "${BOLD}Done.${NC}"
    echo ""
else
    echo ""
    echo -e "${RED}${BOLD}Failed.${NC} ${DIM}Check the container: docker compose logs context-api${NC}"
    echo ""
    exit 1
fi
