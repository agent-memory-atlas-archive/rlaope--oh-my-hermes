"""Read replies out of a Hermes session for the reply lint, read-only.

Hermes keeps every turn in ``state.db`` (``messages``: ``session_id``,
``role``, ``content``, ordered by ``id``). This reader pairs each assistant
reply with the user message it answered, so the lint's carve-out sees what
the person asked, and skips the rows Hermes re-injects after a compaction
(``[PRIOR CONTEXT ...]``), which a person never read as a reply. The
database is opened ``mode=ro``; nothing is written.

``open_state_db_readonly`` is the one place that open is spelled; the
session-usage reader shares it so both surfaces fail the same way on a
missing or unreadable database.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import quote


HERMES_LATEST_SESSION = "latest"
_PRIOR_CONTEXT_PREFIX = "[PRIOR CONTEXT"
_NO_SOURCE_LABEL = "(none)"


class ReplySourceError(ValueError):
    """The session or database could not be read; the message says which."""


def open_state_db_readonly(
    hermes_home: str | Path, *, error: type[ValueError] = ReplySourceError
) -> tuple[Path, sqlite3.Connection]:
    """Open ``<hermes_home>/state.db`` with ``mode=ro`` and return its path and connection.

    A missing file or a refused open raises ``error`` with the path in the
    message; the caller closes the connection.
    """
    path = Path(hermes_home).expanduser() / "state.db"
    if not path.exists():
        raise error(f"no Hermes state database at {path}")
    try:
        connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error as exc:
        raise error(f"could not open {path}: {exc}") from exc
    return path, connection


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
    ``source`` when one is given. An explicit id whose row carries a
    different tag is an error, so a filter never looks applied when it was
    not. ``last`` bounds how many trailing assistant replies are returned,
    newest last.
    """
    if last < 1:
        raise ReplySourceError("--last must be at least 1")
    path, connection = open_state_db_readonly(hermes_home)
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
            actual = (row[0] or _NO_SOURCE_LABEL) if row is not None else None
            if actual is not None and actual != source:
                raise ReplySourceError(f"session {wanted} has source {actual}, not {source}")
        return wanted
    order = "ORDER BY COALESCE(last_activity_at, started_at) DESC, id DESC LIMIT 1"
    if source is None:
        row = connection.execute(f"SELECT id FROM sessions {order}").fetchone()
        if row is None:
            raise ReplySourceError("the Hermes state database has no sessions")
        return str(row[0])
    row = connection.execute(f"SELECT id FROM sessions WHERE source = ? {order}", (source,)).fetchone()
    if row is None:
        raise ReplySourceError(f"no Hermes session with source {source}")
    return str(row[0])
