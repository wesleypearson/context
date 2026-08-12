"""
CRM Ingest
==========

A deterministic (non-agent) endpoint for external services to file a
structured crm.projects row without going through the LLM-mediated
update_crm tool. Appropriate for "a project was created" — that's a typed
event, not something that needs an LLM to decide which table it belongs in
(compare agents/instructions.py's CRM_WRITE, which exists precisely because
free-text filing does need that judgment call).

First caller: critique-tool's critiqueApi (artefact-platform issue #119) —
links a Critique design-review project to the owner's CRM the moment it's
created, with a note pointing back at the share URL.

Auth is a single static key (CRM_INGEST_API_KEY), not a JWT — this is a
service-to-service call from a different codebase/deployment (a Firebase
Cloud Function), not a user session, so there's no `sub` to authorize
against `OWNER_IDS` the way agents/policy.py's is_owner() does for a
person. The row is always filed under CANONICAL_OWNER_ID regardless of
what the caller sends — same posture as the inbound queue (see
app/identity.py's module docstring: "the user_id the inbound queue rows
are written under").
"""

import hmac
from os import getenv

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.identity import CANONICAL_OWNER_ID
from db.session import get_sql_engine

router = APIRouter(prefix="/crm", tags=["crm-ingest"])


class ProjectIngest(BaseModel):
    name: str = Field(..., min_length=1, max_length=500)
    notes: str = Field("", max_length=5000)
    tags: list[str] = Field(default_factory=list)
    status: str = Field("active")


class ProjectIngestResult(BaseModel):
    id: int


def _check_api_key(x_api_key: str | None) -> None:
    expected = getenv("CRM_INGEST_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="CRM_INGEST_API_KEY not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


@router.post("/projects", response_model=ProjectIngestResult)
def ingest_project(
    payload: ProjectIngest, x_api_key: str | None = Header(default=None, alias="X-API-Key")
) -> ProjectIngestResult:
    """File a crm.projects row for the owner, deterministically — no agent involved.

    Uses the same write-guarded, crm-schema-scoped engine as the agent's own
    update_crm tool (get_sql_engine — see db/session.py), so this rides the
    exact same confinement (search_path=crm,public + the foreign-schema
    write guard) without re-implementing it.
    """
    _check_api_key(x_api_key)

    if not CANONICAL_OWNER_ID:
        raise HTTPException(status_code=503, detail="No OWNER_ID configured — nowhere to file this")

    with get_sql_engine().begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO projects (name, status, notes, tags, user_id) "
                "VALUES (:name, :status, :notes, :tags, :user_id) "
                "RETURNING id"
            ),
            {
                "name": payload.name,
                "status": payload.status,
                "notes": payload.notes,
                "tags": payload.tags,
                "user_id": CANONICAL_OWNER_ID,
            },
        ).first()

    return ProjectIngestResult(id=row[0])
