#!/bin/bash

############################################################################
#
#    @context — turnkey: arm MCP OAuth on the deployed instance.
#
#    THE single command to make the deployed @context connectable from MCP
#    clients. Production connections ride MCP OAuth: MCP_CONNECT_SECRET turns
#    the deployment into its own OAuth 2.1 authorization server on /mcp, and
#    every client — claude.ai, ChatGPT, Claude Code, Codex, Cursor — connects
#    by URL and gets approved once on a consent page with that secret.
#    No tokens to mint, nothing to paste into client configs. Runs, in order:
#
#      0. Preflight        — Railway CLI, `railway login`, project link
#      1. Ensure secrets   — MCP_CONNECT_SECRET (the consent-page secret) and
#                            AGENTOS_MCP_SIGNING_KEY (the token signing root)
#                            in .env.production; generated when missing
#      2. Sync env         — railway/env-sync.sh pushes both to the service
#      3. Push to prod     — railway up (redeploys, applying numReplicas:1 from
#                            railway.json — the single-replica fix that makes
#                            the remote MCP session reliable; see
#                            docs/SCALING.md). Skipped with --no-redeploy.
#      4. Wait (optional)  — poll the deploy's /health so it can tell you when
#                            the new build is live (skip with --no-wait)
#
#    Then it prints the connector recipe: the URL to paste into claude.ai /
#    ChatGPT (+ the consent secret), and `uvx agno connect` for the CLI
#    clients. It never restarts your apps and never touches Postgres / data.
#
#    Rotation: re-running keeps the existing secrets. To gate future consents
#    on a new secret, delete MCP_CONNECT_SECRET from .env.production and
#    re-run. To revoke EVERY issued token (the kill switch), delete
#    AGENTOS_MCP_SIGNING_KEY and re-run — all clients re-consent.
#
#    Run from the repo root.
#
#    Usage:
#      ./scripts/setup_context.sh [--no-redeploy] [--no-wait]
#
#      --no-redeploy     skip the `railway up` redeploy (env-sync above still
#                        redeploys when a variable changed)
#      --no-wait         don't poll /health after deploying; exit immediately
#
############################################################################

set -euo pipefail

BOLD='\033[1m'
DIM='\033[2m'
ORANGE='\033[38;5;208m'
GREEN='\033[32m'
RED='\033[31m'
NC='\033[0m'

CURR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${CURR_DIR}")"
cd "${REPO_ROOT}"

# ---- args -----------------------------------------------------------------
WAIT_FOR_HEALTH=1
REDEPLOY=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-redeploy) REDEPLOY=0; shift ;;
        --no-wait) WAIT_FOR_HEALTH=0; shift ;;
        -h|--help)
            cat <<'USAGE'
@context — arm MCP OAuth on the deployed instance (one command).

Usage: ./scripts/setup_context.sh [options]

  Default (full setup): railway login check -> ensure MCP_CONNECT_SECRET +
  AGENTOS_MCP_SIGNING_KEY exist in .env.production (generated when missing) ->
  env-sync -> `railway up` redeploy (applies railway.json, e.g. numReplicas) ->
  wait for /health -> print the connector recipe (paste the /mcp URL into
  claude.ai / ChatGPT and approve the consent page with the secret;
  `uvx agno connect` for CLI clients). Never restarts apps; never touches
  Postgres.

  --no-redeploy    skip the `railway up` redeploy (env-sync still redeploys
                   when a variable changed)
  --no-wait        don't poll /health after deploying; exit immediately
USAGE
            exit 0 ;;
        *) echo "error: unknown option '$1' (try $0 --help)" >&2; exit 2 ;;
    esac
done

step() { echo ""; echo -e "${BOLD}$1${NC}"; }
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${ORANGE}!${NC} $1"; }

# Ensure a KEY=value exists in .env.production, generating the value when the
# key is missing or empty. Replaces a commented-or-empty `KEY=` line in place;
# appends if absent. Prints whether it generated or kept the value.
ensure_env_secret() {
    local key="$1" file=".env.production" existing value tmp
    existing="$(grep -E "^${key}=" "$file" | head -1 | cut -d= -f2- || true)"
    existing="${existing%\"}"; existing="${existing#\"}"
    existing="${existing%\'}"; existing="${existing#\'}"
    if [[ -n "$existing" ]]; then
        ok "${key} already set in ${file} (kept)"
        return
    fi
    value="$(openssl rand -base64 32)"
    if grep -qE "^[#[:space:]]*${key}=" "$file"; then
        tmp="$(mktemp)"
        if sed -E "s|^[#[:space:]]*${key}=.*|${key}=${value}|" "$file" > "$tmp"; then
            cat "$tmp" > "$file"
        fi
        rm -f "$tmp"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$file"
    fi
    ok "${key} generated → ${file}"
}

# ---- banner ---------------------------------------------------------------
echo ""
GRADIENT=(220 214 208 202 166 130)
i=0
while IFS= read -r line; do
    printf '\033[38;5;%dm%s\033[0m\n' "${GRADIENT[$i]}" "$line"
    i=$((i+1))
done << 'BANNER'
     █████╗  ██████╗ ███╗   ██╗ ██████╗
    ██╔══██╗██╔════╝ ████╗  ██║██╔═══██╗
    ███████║██║  ███╗██╔██╗ ██║██║   ██║
    ██╔══██║██║   ██║██║╚██╗██║██║   ██║
    ██║  ██║╚██████╔╝██║ ╚████║╚██████╔╝
    ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝
BANNER
echo ""
echo -e "    ${DIM}@context · connect your MCP clients to production (MCP OAuth).${NC}"

# ---- 0. preflight ---------------------------------------------------------
step "[0/4] Preflight"

if [[ ! -f .env.production ]]; then
    echo -e "  ${RED}✗${NC} .env.production not found. Provision first: ./scripts/railway/up.sh" >&2
    exit 1
fi
ok ".env.production present"

if ! command -v railway &> /dev/null; then
    echo -e "  ${RED}✗${NC} Railway CLI not found. Install: https://docs.railway.app/guides/cli" >&2
    exit 1
fi

# Log in if needed (interactive — opens a browser).
if railway whoami &> /dev/null; then
    ok "Railway: logged in as $(railway whoami 2>/dev/null | sed -E 's/.*as //; s/ .*//')"
else
    warn "Railway: not logged in — launching \`railway login\` (a browser will open)…"
    railway login
fi

if ! railway status &> /dev/null; then
    echo -e "  ${RED}✗${NC} Not linked to a Railway project. Run ./scripts/railway/up.sh first (it provisions + links)." >&2
    exit 1
fi
ok "Railway: linked to $(railway status 2>/dev/null | sed -nE 's/^Project: //p' | head -1)"

# OAuth needs the exact public HTTPS origin — the app refuses to boot with a
# plaintext non-localhost AGENTOS_URL, and the consent flow redirects there.
AGENTOS_URL="$(grep -E '^AGENTOS_URL=' .env.production | head -1 | cut -d= -f2- || true)"
AGENTOS_URL="${AGENTOS_URL%\"}"; AGENTOS_URL="${AGENTOS_URL#\"}"     # strip surrounding "…"
AGENTOS_URL="${AGENTOS_URL%\'}"; AGENTOS_URL="${AGENTOS_URL#\'}"     # strip surrounding '…'
if [[ "$AGENTOS_URL" == https://* ]]; then
    ok "AGENTOS_URL=${AGENTOS_URL} (public HTTPS origin)"
else
    warn "AGENTOS_URL in .env.production is '${AGENTOS_URL:-unset}' — MCP OAuth needs the exact"
    warn "public HTTPS origin (https://<your-domain>). Set it before clients can connect."
fi

# Report the replica count we're about to deploy (1 = reliable remote MCP).
REPLICAS="$(grep -oE '"numReplicas"[[:space:]]*:[[:space:]]*[0-9]+' railway.json | grep -oE '[0-9]+' | head -1 || echo '?')"
if [[ "$REPLICAS" == "1" ]]; then
    ok "railway.json: numReplicas=1 (single replica → reliable MCP sessions)"
else
    warn "railway.json: numReplicas=${REPLICAS}. Remote MCP needs 1 replica to be reliable"
    warn "(no session affinity across replicas → 'Session not found'/502). Set it to 1 in railway.json."
fi

# ---- 1. ensure the OAuth secrets ------------------------------------------
step "[1/4] Ensuring the MCP OAuth secrets in .env.production"
# MCP_CONNECT_SECRET arms the OAuth server and gates the consent page — on a
# single-owner deploy it IS an owner credential, so it's generated, never typed.
# AGENTOS_MCP_SIGNING_KEY pins the HS256 signing root for the issued tokens;
# unset, agno would generate one and persist it in Postgres — pinning it in env
# keeps tokens valid across a database reset and gives you the kill switch
# (rotate it → every issued token is revoked).
ensure_env_secret MCP_CONNECT_SECRET
ensure_env_secret AGENTOS_MCP_SIGNING_KEY

# ---- 2. sync env to Railway -----------------------------------------------
step "[2/4] Syncing .env.production to Railway"
./scripts/railway/env-sync.sh

# ---- 3. push to production ------------------------------------------------
if [[ "$REDEPLOY" == "1" ]]; then
    step "[3/4] Pushing to production (applies numReplicas from railway.json)"
    railway up --service agent-os -d
else
    step "[3/4] Skipping \`railway up\` (--no-redeploy)"
    echo -e "  ${DIM}(env-sync above still redeploys when a variable changed, so a fresh secret${NC}"
    echo -e "  ${DIM}takes effect either way.)${NC}"
fi

# ---- 4. wait for the deploy (optional) ------------------------------------
if [[ "$WAIT_FOR_HEALTH" == "1" && "$AGENTOS_URL" == https://* ]]; then
    step "[4/4] Waiting for the new build to come up (Ctrl-C to skip)"
    echo -e "  ${DIM}Polling ${AGENTOS_URL}/health … a single replica has a brief swap window.${NC}"
    start=$(date +%s); deadline=$(( start + 300 )); seen_down=0; settled=0   # up to 5 minutes
    while [[ $(date +%s) -lt $deadline ]]; do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "${AGENTOS_URL}/health" 2>/dev/null || echo 000)"
        now=$(date +%s)
        if [[ "$code" == "200" ]]; then
            if [[ "$seen_down" == "1" ]]; then
                echo ""; ok "Deploy is live again (${AGENTOS_URL}/health → 200). MCP OAuth is armed and serving."
                settled=1; break
            elif (( now - start >= 60 )); then
                # Never caught a restart blip — either the rollout finished between
                # polls or hasn't started. Healthy for 60s is good enough to stop.
                echo ""; ok "Server healthy at ${AGENTOS_URL}/health. If Railway is still rolling out, give it a minute and confirm it's green before connecting clients."
                settled=1; break
            fi
            printf '  .'
        else
            seen_down=1   # caught the rollout swap; the next 200 is the new build
            printf '  .'
        fi
        sleep 5
    done
    [[ "$settled" == "0" ]] && { echo ""; warn "Still deploying after 5 min — check \`railway logs --service agent-os\`. The recipe below works once it's up."; }
else
    step "[4/4] Skipping health wait"
    echo -e "  ${DIM}The deploy is building on Railway (~3–5 min). Check: railway logs --service agent-os${NC}"
fi

# ---- done: the connector recipe --------------------------------------------
MCP_CONNECT_SECRET="$(grep -E '^MCP_CONNECT_SECRET=' .env.production | head -1 | cut -d= -f2- || true)"
MCP_CONNECT_SECRET="${MCP_CONNECT_SECRET%\"}"; MCP_CONNECT_SECRET="${MCP_CONNECT_SECRET#\"}"
MCP_CONNECT_SECRET="${MCP_CONNECT_SECRET%\'}"; MCP_CONNECT_SECRET="${MCP_CONNECT_SECRET#\'}"

echo ""
echo -e "${BOLD}${GREEN}Done.${NC} MCP OAuth is armed. Connect your clients:"
echo ""
echo -e "${BOLD}claude.ai / ChatGPT (web)${NC}"
echo -e "  Add ${BOLD}${AGENTOS_URL:-https://<your-domain>}/mcp${NC} as a custom connector"
echo -e "  ${DIM}(leave the optional OAuth client ID/secret fields empty).${NC}"
echo -e "  Then click Connect and approve the consent page with this secret:"
echo -e "  ${BOLD}${MCP_CONNECT_SECRET:-<MCP_CONNECT_SECRET from .env.production>}${NC}"
echo ""
echo -e "${BOLD}CLI clients (Claude Code, Codex, Cursor, Claude Desktop)${NC}"
echo -e "  ${BOLD}uvx agno connect --url ${AGENTOS_URL:-https://<your-domain>}${NC}"
echo -e "  ${DIM}wires each client and prints its sign-in step (a browser consent per client).${NC}"
echo -e "  ${DIM}By hand: claude mcp add --transport http context ${AGENTOS_URL:-https://<your-domain>}/mcp${NC}"
echo -e "  ${DIM}         claude mcp login context   (and codex mcp add / codex mcp login)${NC}"
echo ""
echo -e "${DIM}Kill switch: rotate AGENTOS_MCP_SIGNING_KEY (delete it from .env.production and re-run)${NC}"
echo -e "${DIM}to revoke every issued token; rotating MCP_CONNECT_SECRET only gates future consents.${NC}"
echo ""
