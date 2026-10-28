"""Where a copy from the OMH TUI's pager goes: OSC 52, or `pbcopy` on the Mac the screen is on.

OSC 52 is never answered, so nothing confirms it reached the clipboard. It
counts as copied only for a terminal that honours it as it ships (kitty,
Ghostty, WezTerm, Alacritty) and outside tmux, which passes it on only with
`set-clipboard on`. Terminal.app ignores it and iTerm2 takes it only once its
preference allows it, so on a Mac whose screen is the one the TUI runs on (no
SSH between them) the copy goes through `pbcopy` instead, whose exit status
does confirm it. Anywhere else it goes out as OSC 52 and is reported as sent.
`pbcopy` is a fixed argv with the text on stdin, run only for a copy the person
made with the mouse in the pager.
"""

from __future__ import annotations

from collections.abc import Mapping
import subprocess

from .terminal import terminal_name

COPY_OSC52 = "osc52"
COPY_PBCOPY = "pbcopy"
COPY_UNCONFIRMED = "osc52-unconfirmed"
OSC52_TERMINALS = frozenset({"kitty", "ghostty", "WezTerm", "alacritty"})  # by `terminal_name`
SSH_VARIABLES = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")  # set: the screen is on another machine than pbcopy's
PBCOPY_SECONDS = 2.0


def copy_route(environ: Mapping[str, str], platform: str) -> str:
    """How a copy from the pager reaches the clipboard: COPY_OSC52 for a terminal outside tmux that honours OSC 52 as
    it ships, COPY_PBCOPY on a Mac the screen is on (no SSH between them), else COPY_UNCONFIRMED."""
    if terminal_name(environ) in OSC52_TERMINALS and not environ.get("TMUX"):
        return COPY_OSC52
    if platform == "darwin" and not any(environ.get(name) for name in SSH_VARIABLES):
        return COPY_PBCOPY
    return COPY_UNCONFIRMED


def pbcopy(text: str, environ: Mapping[str, str]) -> bool:
    """`text` on the Mac's clipboard through `pbcopy`, read as UTF-8 whatever the locale says; False when it did not
    run or failed."""
    try:
        done = subprocess.run(
            ["pbcopy"], input=text.encode("utf-8", "replace"), env={**environ, "LC_ALL": "en_US.UTF-8"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=PBCOPY_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
