"""Read replies out of a Hermes session for the reply lint, read-only.

Hermes keeps every turn in ``state.db`` (``messages``: ``session_id``,
``role``, ``content``, ordered by ``id``). This reader pairs each assistant
reply with the user message it answered, so the lint's carve-out sees what
the person asked, and skips the rows Hermes re-injects after a compaction
(``[PRIOR CONTEXT ...]``), which a person never read as a reply. The
database is opened ``mode=ro`` through ``hermes_state.open_state_db_readonly``,
the open the session-usage reader shares, so the two quality readers fail the
same way on a missing or unreadable database; nothing is written.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from .hermes_state import NO_SOURCE_LABEL, open_state_db_readonly, session_source_clause


HERMES_LATEST_SESSION = "latest"
_PRIOR_CONTEXT_PREFIX = "[PRIOR CONTEXT"


class ReplySourceError(ValueError):
    """The session or database could not be read; the message says which."""


def hermes_session_replies(
    hermes_home: str | Path,
    session_id: str,
    *,
    last: int = 1,
    source: str | None = None,
) -> dict[str, Any]:
    """Return ``{"session_id", "replies": [{"user_text", "reply", "message_id"}]}``.

    ``session_id`` may be ``latest``, which resolves to the most recently
    active session row -- the most recent row whose ``source`` tag equals
    ``source`` when one is given (``NO_SOURCE_LABEL`` selects untagged rows).
    An explicit id whose row carries a different tag, or that has no session
    row to check the tag on, is an error, so a filter never looks applied
    when it was not. ``last`` bounds how many trailing assistant replies are
    returned, newest last.
    """
    if last < 1:
        raise ReplySourceError("--last must be at least 1")
    path, connection = open_state_db_readonly(hermes_home, error=ReplySourceError)
    try:
        resolved = _resolve_session(connection, session_id, source)
        rows = connection.execute(
            "SELECT id, role, content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant') ORDER BY id",
            (resolved,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ReplySourceError(f"could not read {path}: {exc}") from exc
    finally:
        connection.close()
    if not rows:
        raise ReplySourceError(f"session {resolved} has no user or assistant messages")
    replies: list[dict[str, Any]] = []
    user_text = ""
    for message_id, role, content in rows:
        text = str(content or "")
        if role == "user":
            user_text = text
            continue
        if text.startswith(_PRIOR_CONTEXT_PREFIX) or not text.strip():
            continue
        if replies and replies[-1]["reply"] == text:
            # A compaction can re-record the same reply under a new id.
            continue
        replies.append({"message_id": int(message_id), "user_text": user_text, "reply": text})
    return {"session_id": resolved, "replies": replies[-last:]}


def _resolve_session(connection: sqlite3.Connection, session_id: str, source: str | None) -> str:
    wanted = str(session_id or "").strip()
    if not wanted:
        raise ReplySourceError("a session id (or `latest`) is required")
    if wanted != HERMES_LATEST_SESSION:
        if source is not None:
            row = connection.execute("SELECT source FROM sessions WHERE id = ?", (wanted,)).fetchone()
            if row is None:
                raise ReplySourceError(f"no Hermes session {wanted} to check --source {source} against")
            actual = row[0] or NO_SOURCE_LABEL
            if actual != source:
                raise ReplySourceError(f"session {wanted} has source {actual}, not {source}")
        return wanted
    order = "ORDER BY COALESCE(last_activity_at, started_at) DESC, id DESC LIMIT 1"
    if source is None:
        row = connection.execute(f"SELECT id FROM sessions {order}").fetchone()
        if row is None:
            raise ReplySourceError("the Hermes state database has no sessions")
        return str(row[0])
    clause, params = session_source_clause(source)
    row = connection.execute(f"SELECT id FROM sessions WHERE {clause} {order}", params).fetchone()
    if row is None:
        raise ReplySourceError(f"no Hermes session with source {source}")
    return str(row[0])
