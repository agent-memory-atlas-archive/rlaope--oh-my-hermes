"""Primitives for reading Hermes' own ``state.db``: the read-only open, the
``sessions.source`` filter, and the stamp parser.

Hermes keeps sessions and messages in ``<hermes_home>/state.db``; the OMH
readers observe it and none writes. The reply lint and the session-usage
report in this package open it through ``open_state_db_readonly``, so both
fail the same way on a missing or unreadable database. The menubar session
reader in ``surfaces.hermes_sessions`` keeps its own open (it reports
``unobserved`` rather than raising) but parses the same stamp column through
``hermes_epoch``, so there is one parser to change when Hermes changes the
stamp shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import quote


NO_SOURCE_LABEL = "(none)"


def open_state_db_readonly(
    hermes_home: str | Path, *, error: type[ValueError]
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


def session_source_clause(source: str) -> tuple[str, tuple[str, ...]]:
    """The ``WHERE`` predicate for a ``sessions.source`` filter and its parameters.

    ``NO_SOURCE_LABEL`` is what the readers print for a row without a tag, so
    as a filter it selects those rows; any other value must equal the tag.
    """
    if source == NO_SOURCE_LABEL:
        return "(source IS NULL OR source = '')", ()
    return "source = ?", (source,)


def hermes_epoch(value: Any) -> float | None:
    """Epoch seconds from a Hermes stamp, or ``None`` when it is not one.

    Hermes stores REAL epoch seconds; older rows and fixtures carry ISO-8601
    text. A non-finite number is not a stamp in either form: ``nan`` text
    would otherwise disable a ``--since`` window and reach a JSON payload as
    a token ``json`` cannot read back.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        return number if math.isfinite(number) else None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
