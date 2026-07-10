"""
Notifications
=============

A single outbound channel for proactive Slack DMs — deterministic (no model, no
approval), best-effort, and email-addressed.

Two callers, two directions:
- `dm_owner` — the self-notification path: the reminder sweep and the scheduled
  digests DM the owner their own follow-ups and briefs. Ungated because you are
  messaging yourself, not acting on the outside world.
- `dm_user` — the receipt path: when the owner acknowledges a teammate's update,
  the teammate gets a one-line "seen" DM. Ungated for the same reason messaging
  is (docs/SECURITY.md): a receipt confirms delivery of the sender's own
  message; it carries no owner data.

Both are distinct from the `update_slack` *tool*, which the agent uses to message
people and channels on request.

No-op (returns False) unless Slack DMs are actually available: `SLACK_BOT_TOKEN`
(the bot token that sends) and an email to resolve via `users.lookupByEmail`
(for the owner: an `OWNER_ID` entry that looks like one). `SLACK_SIGNING_SECRET`
only verifies *inbound* Slack requests, so it plays no part here — sending needs
just the token, with the `users:read.email`, `im:write`, `chat:write` scopes.
"""

from os import getenv

from agno.utils.log import log_warning

from app.identity import owner_email


def slack_dm_target() -> tuple[str, str] | None:
    """The (bot token, owner email) an owner DM needs — or `None` when unavailable."""
    token = getenv("SLACK_BOT_TOKEN")
    email = owner_email()
    if token and email:
        return token, email
    return None


def dm_user(email: str, text: str) -> bool:
    """Best-effort: DM the Slack user behind `email`. Returns whether it was sent.

    Every failure is logged and swallowed — callers treat the DM as a nudge layered
    on top of a durable source of truth (the inbound queue), never as the delivery
    guarantee itself. No-op when the bot token isn't set or `email` doesn't resolve
    to a workspace member.
    """
    token = getenv("SLACK_BOT_TOKEN")
    if not (token and email and "@" in email):
        return False
    try:
        from slack_sdk import WebClient

        client = WebClient(token=token)
        user_id = client.users_lookupByEmail(email=email)["user"]["id"]
        channel = client.conversations_open(users=[user_id])["channel"]["id"]
        client.chat_postMessage(channel=channel, text=text)
        return True
    except Exception as exc:
        log_warning(f"dm_user: could not DM {email} on Slack: {exc}")
        return False


def dm_owner(text: str) -> bool:
    """Best-effort: DM the owner `text` on Slack. Returns whether it was sent.

    The self-notification path (see module docstring). No-op when Slack DMs
    aren't configured.
    """
    target = slack_dm_target()
    if target is None:
        return False
    _, email = target
    return dm_user(email, text)
