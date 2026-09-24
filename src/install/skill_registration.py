"""Which managed skills directory a Hermes home registers, and whether it is one.

`omh setup` and `omh update` register one OMH-managed skills directory in a
Hermes home's `skills.external_dirs`: the shared generation pointer on an
installer-managed command, otherwise the OMH home's own `skills` directory.
Which of them a given home recorded depends only on when it was registered,
so "registered" reads any of them -- a profile left on the older path was
once scored as opted out and frozen on the generation it was installed at.

Two of them at once is a different state, not a wider registration. Hermes
resolves a bare skill name across every external directory and refuses one
that resolves to two different files ("Ambiguous skill name ... Refusing to
guess"); only copies that resolve to the same file count as one. Every
generation refresh makes the pre-pointer copy and the pointer differ, so a
home naming both loses every OMH skill by name (#1857). Setup and update
migrate such a home to the one path the running command writes; `omh doctor`
and the update post-check report a home that still names two, in the same
words. This module is the one place both sides read the candidate set and
spell the finding, so the writer and the readers cannot drift.

Pure path and text work: no config write, no subprocess, no network.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import os
from pathlib import Path

from .config_adapter import ConfigChange, ensure_external_dir, external_dirs, remove_external_dir
from ..paths import OmhPaths
from ..plugin_bundle.omh import runtime_paths

AMBIGUOUS_REGISTRATION_NEXT_ACTION = "run `omh update`"


def external_dir_key(path: str | Path) -> str:
    """One comparable spelling of a registered directory across separators and case."""
    return os.path.normcase(os.path.normpath(str(path))).replace("\\", "/")


def managed_skill_dir_candidates(paths: OmhPaths, *, current: Path | None) -> list[Path]:
    """Every managed skills directory a registration may legitimately name.

    `<omh_home>/skills` is listed by name, not only through `paths.skills_dir`:
    on a managed command install `resolve_paths` redirects `skills_dir` to the
    running generation's pack, so the pre-pointer registration home -- the
    path a frozen profile carries -- would otherwise never be a candidate on
    exactly the machines that need it. The standalone default store is named
    too: a profile that later selected its own store was registered by the
    sync at the primary's, which for a default install is that one.

    `current` is the shared generation pointer as the caller resolved it,
    passed in rather than read here so the installer's own binding (and the
    fixtures that stub it) stays the one reading.
    """
    candidates: list[Path] = []
    for candidate in (
        paths.omh_home / "skills",
        paths.skills_dir,
        current,
        runtime_paths.standalone_default_omh_home(paths.hermes_home) / "skills",
    ):
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    return candidates


@dataclass(frozen=True)
class RegistrationMigration:
    """One home's registration after the migration: the text to write and what moved."""

    text: str
    added: bool
    message: str
    retired: list[str]

    @property
    def registration(self) -> str:
        """`migrated` when an older managed entry was retired, `added` when only
        today's path went in, else `unchanged` -- the word setup's apply step
        and each profile row carry."""
        if self.retired:
            return "migrated"
        return "added" if self.added else "unchanged"


def migrate_managed_registration(
    config_text: str, registered_dir: Path, candidates: Iterable[Path]
) -> RegistrationMigration:
    """Register `registered_dir` and retire every OTHER managed candidate, in one text.

    A migration, not an unregistration: the home ends registered at exactly
    one managed path. A home carried forward from the pre-pointer path used
    to keep `<omh_home>/skills` beside the generation pointer, and Hermes
    refuses a bare skill name that resolves to two different files -- so
    every generation refresh made every OMH skill fail to load by name in
    that home (#1857). Only the candidates given are retired; a directory the
    person registered themselves is not OMH's to touch. Retirement matches
    the entry's text the way registration wrote it (`remove_external_dir`),
    so an older entry spelled some other way stays, and the readers of
    `registered_managed_entries` name that home afterwards. Whether a home
    should be registered at all is the caller's question: this function
    always writes today's path, and the opt-out rule (a home naming no
    managed directory is left alone) is applied before it is called.

    Pure text work, so it can be run against a copy of a live config.
    """
    change = ensure_external_dir(config_text, registered_dir)
    text = change.text
    retired: list[str] = []
    registered_key = external_dir_key(registered_dir)
    for candidate in candidates:
        if external_dir_key(candidate) == registered_key:
            continue
        removal: ConfigChange = remove_external_dir(text, candidate)
        if removal.changed:
            retired.append(candidate.expanduser().as_posix())
            text = removal.text
    return RegistrationMigration(text=text, added=change.changed, message=change.message, retired=retired)


def _real_key(path: str | Path) -> str:
    expanded = os.path.expanduser(str(path))
    try:
        return external_dir_key(os.path.realpath(expanded))
    except OSError:
        return external_dir_key(expanded)


def registered_managed_entries(config_text: str, candidates: Iterable[Path]) -> list[str]:
    """The registered entries that name a managed directory, one per real directory.

    By text or by real path, the way `external_dir_registered` reads a single
    candidate. Two entries that resolve to the same directory -- the pointer
    and the generation it points at -- are one SKILL.md to Hermes and count
    once; two that resolve to different directories are the ambiguity. Config
    order is kept so a message names them the way the file does.
    """
    keys = {external_dir_key(candidate) for candidate in candidates}
    reals = {_real_key(candidate) for candidate in candidates}
    seen: set[str] = set()
    entries: list[str] = []
    for entry in external_dirs(config_text):
        real = _real_key(entry)
        if external_dir_key(entry) not in keys and real not in reals:
            continue
        if real in seen:
            continue
        seen.add(real)
        entries.append(entry)
    return entries


def registration_home_label(config_path: Path, *, profile: str | None = None) -> str:
    """How doctor and the update post-check both name a home in the finding."""
    if profile is None:
        return str(config_path)
    return f"profile {profile} ({config_path})"


def ambiguous_registration_message(label: str, entries: list[str]) -> str:
    """The one sentence for a home that names two managed skills directories."""
    listed = " and ".join(entries) if len(entries) == 2 else ", ".join(entries)
    return (
        f"{label} names {len(entries)} OMH-managed skills directories in skills.external_dirs ({listed}); "
        "Hermes refuses a bare skill name that resolves to two different files, "
        "so OMH skills fail to load by name"
    )


def hermes_profile_dirs(hermes_home: Path) -> list[tuple[str, Path]]:
    """List the Hermes bot-profile homes under ``<hermes_home>/profiles/``.

    Hermes profiles (``hermes profile create``, Desktop bot chats) are fully
    independent HERMES_HOME directories with their own config.yaml, skills,
    and skins -- a registration written to the primary home never reaches
    them, which is why bot chats showed zero OMH skills while the default
    chat had the full set. Sorted for deterministic output; hidden entries
    are Hermes-internal, never profiles.
    """
    root = hermes_home / "profiles"
    try:
        entries = sorted(entry for entry in root.iterdir() if entry.is_dir() and not entry.name.startswith("."))
    except OSError:
        return []
    return [(entry.name, entry) for entry in entries]
