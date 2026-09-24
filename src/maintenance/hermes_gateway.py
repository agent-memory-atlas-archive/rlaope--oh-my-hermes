"""Does a running Hermes gateway predate the OMH bundle its home now carries?

A gateway keeps serving the plugin bundle and skills it loaded at start, so
after `omh update` a bot that was running through the update still answers
with pre-update code until someone restarts it. Update already says
"restart Hermes"; this module says which gateways that sentence is about,
from file evidence alone.

Hermes writes ``<hermes_home>/gateway.pid`` when a gateway starts (a JSON
record: ``pid``, ``kind``, ``argv``, ``start_time``, ``hermes_home``, created
O_EXCL and unlinked on a clean stop) and appends one epoch-seconds line per
start to ``<hermes_home>/gateway-starts.log`` (its respawn-storm ledger: a
bounded ring of ``repr(float)`` lines). The pid record's ``start_time`` is
NOT a wall clock and is never read as one here: measured on the owner
machine on 2026-09-24 it is psutil ``create_time() * 100`` (centiseconds)
on macOS, and Hermes reads ``/proc/<pid>/stat`` field 22 (clock ticks since
boot) on Linux -- a per-host PID-reuse fingerprint. The starts ledger
carries the wall clock, so the comparison reads that.

No signal, no process listing, no subprocess. A pid record Hermes left
behind after a crash produces a hint to restart a gateway that is not
running, which costs nothing; a missing record produces no hint even when
a gateway runs under some other supervision, which is the evidence
boundary and is said as such. A record naming a different ``hermes_home``
is another profile's, exactly as Hermes' own cross-profile guard reads it,
and produces nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path

from ..install.plugin_pack import read_plugin_manifest
from ..local_store import read_json_object
from ..paths import OmhPaths
from ..install.skill_registration import hermes_profile_dirs

GATEWAY_PID_FILENAME = "gateway.pid"
GATEWAY_STARTS_FILENAME = "gateway-starts.log"


def gateway_restart_hints(paths: OmhPaths) -> list[str]:
    """One line per home whose recorded gateway started before its bundle was installed."""
    hints: list[str] = []
    primary = gateway_restart_hint(
        paths.hermes_home,
        label=str(paths.hermes_home),
        restart_command="hermes gateway restart",
    )
    if primary:
        hints.append(primary)
    for name, profile_dir in hermes_profile_dirs(paths.hermes_home):
        hint = gateway_restart_hint(
            profile_dir,
            label=f"profile {name}",
            restart_command=f"hermes --profile {name} gateway restart",
        )
        if hint:
            hints.append(hint)
    return hints


def gateway_restart_hint(hermes_home: Path, *, label: str, restart_command: str) -> str | None:
    """The hint for one home, or None when the files do not prove a stale gateway."""
    if not _pid_record_names_home(hermes_home):
        return None
    last_start = _last_gateway_start(hermes_home / GATEWAY_STARTS_FILENAME)
    if last_start is None:
        return None
    manifest = read_plugin_manifest(hermes_home / "plugins" / "omh")
    installed_at = _installed_at_epoch(manifest)
    if installed_at is None or last_start >= installed_at:
        return None
    return (
        f"Hermes gateway for {label} started {_iso_utc(last_start)} before this bundle was "
        f"installed ({_iso_utc(installed_at)}); run `{restart_command}`"
    )


def _pid_record_names_home(hermes_home: Path) -> bool:
    try:
        record = read_json_object(hermes_home / GATEWAY_PID_FILENAME)
    except (OSError, ValueError):
        return False
    if record is None:
        return False
    recorded_home = record.get("hermes_home")
    if not isinstance(recorded_home, str) or not recorded_home.strip():
        # A legacy record without a home proves nothing about another
        # profile, so it is read as this home's -- Hermes reads it the same way.
        return True
    return _same_home(recorded_home, hermes_home)


def _same_home(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(left)))) == os.path.normcase(
        os.path.realpath(os.path.expanduser(str(right)))
    )


def _last_gateway_start(path: Path) -> float | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    starts: list[float] = []
    for line in lines:
        try:
            starts.append(float(line.strip()))
        except ValueError:
            continue
    return max(starts) if starts else None


def _installed_at_epoch(manifest: dict[str, object] | None) -> float | None:
    if not manifest:
        return None
    value = manifest.get("installed_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
