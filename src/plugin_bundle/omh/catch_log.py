"""The moments OMH caught a model mistake, kept so the OMH TUI can say so.

Every catch below already reaches the model: a refused repeat call, a note on
a refused re-read, a turn continued over an open plan, a directive on a remote
wait nothing will wake. None of them reached the PERSON, so an install looked
like it did nothing. This ledger is the one fact the TUI needs to print a
single dim row at the moment a catch happens: which kind, in which session,
in what order, and a number or two to word it with.

What it is not. It is not the catch itself -- every hook decides and returns
exactly what it did before, and this record is written beside that decision.
It is not evidence that the model read or followed anything. And it holds no
content: a kind from a closed set and small non-negative integers, never model
text, tool arguments, a path, or a file body. A field a kind does not declare
is refused, not dropped, so a caller cannot widen the record by accident.

Each hook records once per event, on its own latch (the first refusal of a
repeat streak, the first note on a truncated window, the first continuation
of a turn, the turn's one unarmed-wait directive), so a row means "this
happened", never "this fired again".

``seq`` is one counter for the whole file rather than one per session. A
reader keeps the highest ``seq`` it has shown and prints only newer ones; a
per-session counter would restart at 1 after the session's list was evicted,
and every catch after that would read as already shown.

Best-effort like every writer in this bundle: a failure to record is counted
in ``record_failures`` by exception type and never raised, because a hook that
raised would disappear into the host's own try/except and could take the catch
it was describing with it.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any, Final

from . import runtime_paths
# Reuse the awareness ledger's portable lock and atomic-write primitives so
# there is exactly one file-locking implementation in the plugin.
from .awareness_delivery import _awareness_delivery_lock, _write_delivery_record
# The bound the plan record applies to a session reference, reused so a key
# here and every other per-session map in this bundle describe the same string.
from .todo_store import MAX_TODO_SESSION_REF_CHARS

CATCH_LOG_SCHEMA_VERSION: Final = "omh_catch_log/v1"
CATCH_LOG_FILE: Final = "catches.json"

REPEAT_LOOP: Final = "repeat_loop"
TRUNCATED_READ: Final = "truncated_read"
PLAN_CONTINUED: Final = "plan_continued"
UNARMED_WAIT: Final = "unarmed_wait"
# The closed set: each kind and the integer fields it carries, all of them.
CATCH_KINDS: Final[dict[str, tuple[str, ...]]] = {
    # A cycle of `period` calls the host ran `laps` times before the guard
    # refused the next one.
    REPEAT_LOOP: ("period", "laps"),
    # The line a truncated read stopped before, which the note pointed at.
    TRUNCATED_READ: ("offset",),
    # Plan items still open when the turn tried to stop.
    PLAN_CONTINUED: ("open_items",),
    UNARMED_WAIT: (),
}

# A reader shows the newest few; past this a session's older catches have
# already been shown or never will be. Sixty-four sessions is the ceiling
# every other per-session map in this bundle uses.
MAX_CATCHES_PER_SESSION: Final = 8
MAX_CATCH_SESSIONS: Final = 64
# Capped like every other counter in this bundle: past this the exact number
# has stopped meaning anything.
MAX_CATCH_NUMBER: Final = 10**9

# Why a record was not written, by exception type. Process-local, read by
# diagnostics and tests; the hooks never look at it.
record_failures: "Counter[str]" = Counter()


def catch_log_path(omh_home: str = "") -> Path:
    """The ledger under the OMH home a native callback is bound to."""
    return runtime_paths.plugin_home(omh_home or None) / "runtime" / CATCH_LOG_FILE


def record_catch(omh_home: str, session_id: object, kind: str, **numbers: object) -> None:
    """Append one catch to this session's list. Never raises, never returns a value.

    A call that does not fit the closed shape -- an unknown kind, a missing or
    extra field, a value that is not a non-negative integer, no session --
    records nothing: a row the reader could not word is worse than none.
    """
    session = _session_key(session_id)
    fields = CATCH_KINDS.get(kind)
    if not session or fields is None or set(numbers) != set(fields):
        return
    values: dict[str, int] = {}
    for name in fields:
        value = numbers[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return
        values[name] = min(value, MAX_CATCH_NUMBER)
    try:
        path = catch_log_path(omh_home)
        with _awareness_delivery_lock(path):
            stored = _read_file(path)
            seq = min(stored["next_seq"], MAX_CATCH_NUMBER)
            now = time.time()
            sessions = stored["sessions"]
            catches = sessions.pop(session, {}).get("catches", [])
            catches = [*catches, {"seq": seq, "ts": now, "kind": kind, **values}][-MAX_CATCHES_PER_SESSION:]
            sessions[session] = {"ts": now, "catches": catches}
            if len(sessions) > MAX_CATCH_SESSIONS:
                newest = sorted(sessions.items(), key=lambda item: item[1]["ts"], reverse=True)
                sessions = dict(newest[:MAX_CATCH_SESSIONS])
            _write_delivery_record(
                path,
                {
                    "schema_version": CATCH_LOG_SCHEMA_VERSION,
                    "next_seq": seq + 1,
                    "sessions": sessions,
                    "privacy": "metadata_only",
                },
            )
    except (OSError, ValueError, TypeError, RuntimeError, ArithmeticError) as exc:
        # `TimeoutError` (the lock) is an `OSError`; an unbindable home is a
        # `RuntimeBindingError`, a `ValueError`; a symlinked state root is a
        # `RuntimeError`, as is the `RecursionError` of a hand-nested file; a
        # hand-edited number too large for a float is an `ArithmeticError`. Counted, so a ledger that stopped recording is
        # distinguishable from a session OMH caught nothing in.
        record_failures[type(exc).__name__] += 1


def read_catches(omh_home: str | Path, session_id: object, *, limit: int) -> list[dict[str, int | str]]:
    """This session's newest ``limit`` catches, oldest first, or ``[]``.

    Read with an explicit home: the reader is the HUD, which already resolved
    the one it reads. Each entry is re-checked against the closed shape, so a
    hand-edited file can hand a surface nothing it could not word.
    """
    session = _session_key(session_id)
    if not session or limit <= 0:
        return []
    try:
        stored = _read_file(Path(omh_home) / "runtime" / CATCH_LOG_FILE)
    except (OSError, ValueError, TypeError, RecursionError, ArithmeticError):
        # A hand-edited file nested past the parser's depth, or holding a number
        # too large for a float, reads as nothing rather than failing the HUD.
        return []
    entries = stored["sessions"].get(session, {}).get("catches", [])
    return [{key: value for key, value in entry.items() if key != "ts"} for entry in entries][-limit:]


def _read_file(path: Path) -> dict[str, Any]:
    """The file's counter and its well-formed rows; an absent or unparseable file is empty.

    Unparseable reads as empty rather than failing, so the next write replaces
    it instead of every write failing on it for good. That restarts ``seq``,
    which costs a reader the rows until the counter passes its mark again,
    never a row shown twice.
    """
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    next_seq = raw.get("next_seq")
    sessions: dict[str, dict[str, Any]] = {}
    source = raw.get("sessions")
    for session, row in (source.items() if isinstance(source, dict) else ()):
        if not isinstance(session, str) or not isinstance(row, dict):
            continue
        listed = row.get("catches")
        catches = [entry for entry in listed if _well_formed(entry)] if isinstance(listed, list) else []
        ts = row.get("ts")
        if catches:
            sessions[session] = {
                "ts": _finite(ts),
                "catches": catches[-MAX_CATCHES_PER_SESSION:],
            }
    return {
        "next_seq": next_seq if isinstance(next_seq, int) and not isinstance(next_seq, bool) and next_seq > 0 else 1,
        "sessions": sessions,
    }


def _finite(value: object) -> float:
    """``value`` as a finite float, or 0.0: a hand-edited ``ts`` such as ``10**400`` must not raise on the way in."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except OverflowError:
        return 0.0
    return number if math.isfinite(number) else 0.0


def _well_formed(entry: object) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
        return False
    fields = CATCH_KINDS.get(entry["kind"])
    if fields is None or set(entry) != {"seq", "ts", "kind", *fields}:
        return False
    numbers = [entry[name] for name in ("seq", *fields)]
    return all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in numbers) and (
        isinstance(entry["ts"], (int, float)) and not isinstance(entry["ts"], bool)
    )


def _session_key(session_id: object) -> str:
    if not isinstance(session_id, str):
        return ""
    return session_id.strip()[:MAX_TODO_SESSION_REF_CHARS]
