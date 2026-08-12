"""
AgentOS Entrypoint
==================
"""

from contextlib import asynccontextmanager
from os import getenv
from pathlib import Path

from agno.os import AgentOS
from agno.os.config import AuthorizationConfig
from agno.utils.log import log_info

from agents.context import context
from agents.sources import close_context_providers, setup_context_providers, size_io_thread_pool
from app.crm_ingest import router as crm_ingest_router
from app.mcp import context_mcp_config
from app.schedules import register_schedules
from app.settings import is_prd, runtime_env, warn_on_missing_config
from db import create_tables, get_postgres_db
from workflows import WORKFLOWS

# One database session for AgentOS persistence. (The scheduler and workflows open their own sessions)
db = get_postgres_db()

# Where cron triggers reach AgentOS, and — when MCP OAuth is enabled — the public
# origin the OAuth server advertises (set your public URL in prod).
agentos_url = getenv("AGENTOS_URL", "http://127.0.0.1:8000")

# MCP OAuth — armed by setting MCP_CONNECT_SECRET. The deployment becomes its own
# OAuth 2.1 authorization server on /mcp: claude.ai, ChatGPT, and the local MCP
# clients connect by URL and you approve each one on a consent page with this
# secret. Tokens arrive as the reserved `__oauth__:<client_id>` principal, which
# the owner gate honors (see app/mcp.py — only your consent can mint one). Unset,
# /mcp is unchanged (JWT in prod, keyless-as-owner in dev).
mcp_auth = None
if getenv("MCP_CONNECT_SECRET"):
    from agno.os import AgentOSBuiltinAuth

    mcp_auth = AgentOSBuiltinAuth(
        url=agentos_url,
        secret=getenv("MCP_CONNECT_SECRET", ""),
        signing_key_material=getenv("AGENTOS_MCP_SIGNING_KEY"),
    )


def _build_interfaces() -> list:
    """@context's external interfaces — Slack is added when its credentials are set."""
    token = getenv("SLACK_BOT_TOKEN", "")
    signing_secret = getenv("SLACK_SIGNING_SECRET", "")
    if not (token and signing_secret):
        return []

    from agno.os.interfaces.slack import Slack

    return [
        Slack(
            agent=context,
            streaming=True,
            token=token,
            signing_secret=signing_secret,
            resolve_user_identity=True,
            # Quick prompts in the assistant pane. Slack shows these to every user
            # identically (the thread-started event carries no identity), so lead
            # with the one action everyone can take; the owner-only prompts follow.
            # Mirrors the quick prompts in app/config.yaml.
            suggested_prompts=[
                {
                    "title": "Leave an update",
                    "message": "Met Kyle from Agno, wants a partnership — follow up next week",
                },
                {"title": "Daily rundown", "message": "Give me a rundown of what's waiting on me"},
                {"title": "My week", "message": "What does my week look like?"},
            ],
        )
    ]


interfaces = _build_interfaces()


@asynccontextmanager
async def lifespan(app):  # type: ignore[no-untyped-def]
    """App startup: warn on missing config, size the I/O thread pool, create tables,
    register schedules, set up providers. Shutdown: release provider resources."""
    log_info("@context: startup")
    warn_on_missing_config()
    size_io_thread_pool()
    create_tables()
    register_schedules()
    await setup_context_providers()
    try:
        yield
    finally:
        await close_context_providers()
        log_info("@context: shutdown")


# User isolation scopes the OS REST endpoints (sessions / memory / runs) to the
# verified JWT user. Only takes effect when authorization is on (prod).
authorization_config = AuthorizationConfig(user_isolation=True)

agent_os = AgentOS(
    tracing=True,
    scheduler=True,
    lifespan=lifespan,
    db=db,
    agents=[context],
    workflows=WORKFLOWS,
    interfaces=interfaces,
    config=str(Path(__file__).parent / "config.yaml"),  # Quick prompts for the agents.
    authorization=is_prd(),  # JWT authorization in production.
    authorization_config=authorization_config,
    scheduler_base_url=agentos_url,
    internal_service_token=getenv("INTERNAL_SERVICE_TOKEN") or None,
    # Owner-only single-tool MCP server at /mcp — see app/mcp.py.
    mcp_server=context_mcp_config(),
    mcp_auth=mcp_auth,
)
app = agent_os.get_app()
log_info(f"@context: owner-only MCP server mounted at /mcp (OAuth {'on' if mcp_auth else 'off'})")

# Deterministic (non-agent) ingest routes — see app/crm_ingest.py's module
# docstring for why this bypasses the LLM-mediated update_crm tool.
app.include_router(crm_ingest_router)


if __name__ == "__main__":
    agent_os.serve(app="app.main:app", reload=runtime_env() == "dev")
