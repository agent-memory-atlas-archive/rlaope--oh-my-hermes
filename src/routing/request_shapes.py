"""Request shapes: an imperative verb acting on the object a skill owns.

Shortlist-first routing dispatches only on strong evidence, and a trigger
token is not strong evidence: `review` alone is a word twenty skills list.
What is strong is the SHAPE of a request -- a review verb whose object is a
pull request or a diff, a deploy verb whose target is production, a filing
verb whose object is a bug and whose destination is GitHub. Each shape here
is built from vocabulary (verbs, objects, contexts), not from any one
sentence, and each needs the verb in command position: the first word after
an optional courtesy opener ("please", "can you"). "The review of our budget
changes is due Friday" names a review; it does not ask for one.

A shape adds a `direct:` label in `recommend._score_definition`, which the
dispatch-evidence gate reads as strong evidence. Pure functions over the
normalized message and its tokens; stdlib only.
"""

from __future__ import annotations

import re

_COURTESY_OPENERS = (
    "please ",
    "pls ",
    "can you ",
    "could you ",
    "would you ",
    "will you ",
    "can we ",
    "could we ",
    "kindly ",
    "hey ",
    "ok ",
    "okay ",
    "now ",
    # Addressing the assistant by name before the command.
    "omh ",
    "hermes ",
    "hermes, ",
)
_WORD = re.compile(r"[a-z0-9#][a-z0-9#+.-]*")


def command_words(normalized_query: str) -> list[str]:
    """The message's words from its command verb on, courtesy openers removed."""
    text = normalized_query.strip()
    changed = True
    while changed:
        changed = False
        for opener in _COURTESY_OPENERS:
            if text.startswith(opener):
                text = text[len(opener) :].lstrip(" ,")
                changed = True
    return _WORD.findall(text)


# --- review: a review verb on a change set --------------------------------
_REVIEW_VERBS = frozenset({"review", "check", "audit", "inspect"})
_REVIEW_PHRASAL_VERBS = (("look", "over"), ("go", "over"), ("look", "at"), ("read", "through"))
_REVIEW_OBJECT_TOKENS = frozenset(
    {"pr", "prs", "diff", "diffs", "patch", "patches", "commit", "commits", "changeset", "changes", "change"}
)
_REVIEW_OBJECT_PHRASES = ("pull request", "merge request")


def _verb_then(words: list[str], verbs: frozenset[str], phrasal: tuple[tuple[str, str], ...] = ()) -> list[str] | None:
    if not words:
        return None
    if words[0] in verbs:
        return words[1:]
    for first, second in phrasal:
        if len(words) > 1 and words[0] == first and words[1] == second:
            return words[2:]
    return None


def code_review_shape(normalized_query: str) -> bool:
    """"review PR 1234", "please review the diff", "go over the commits in this branch"."""
    rest = _verb_then(command_words(normalized_query), _REVIEW_VERBS, _REVIEW_PHRASAL_VERBS)
    if rest is None:
        return False
    tail = " ".join(rest)
    return bool(_REVIEW_OBJECT_TOKENS & set(rest)) or any(phrase in tail for phrase in _REVIEW_OBJECT_PHRASES)


# --- build failure: a failure reported for a build in a code context -------
_FAILURE_WORDS = frozenset(
    {
        "error", "errors", "erroring", "fail", "fails", "failed", "failing", "failure", "failures",
        "crash", "crashes", "crashed", "crashing", "exception", "exceptions", "throw", "throws",
        "thrown", "warning", "warnings", "traceback", "complaint", "complaints", "broken", "red",
    }
)
_BUILD_SUBJECT_WORDS = frozenset({"build", "builds", "ci", "pipeline", "compile", "compilation", "typecheck", "lint"})
_CODE_CONTEXT_WORDS = frozenset(
    {
        "ci", "main", "master", "branch", "pr", "pipeline", "release", "npm", "yarn", "pnpm", "gradle",
        "maven", "mvn", "cargo", "tsc", "typescript", "webpack", "vite", "make", "docker", "github",
        "actions", "compile", "compilation", "typecheck", "lint",
    }
)


def build_failure_shape(query_tokens: set[str]) -> bool:
    """A build subject, a failure word, and a code context: "the CI build is failing on main"."""
    return (
        bool(_BUILD_SUBJECT_WORDS & query_tokens)
        and bool(_FAILURE_WORDS & query_tokens)
        and bool(_CODE_CONTEXT_WORDS & query_tokens)
    )


# --- deploy: a deploy verb with an environment target ----------------------
_DEPLOY_VERBS = frozenset({"deploy", "ship", "release", "promote"})
_DEPLOY_PHRASAL_VERBS = (("roll", "out"), ("push", "to"))
# A release to users is what deploy-and-monitor watches; a staging deploy is
# part of the development loop.
_DEPLOY_TARGETS = frozenset({"production", "prod", "live"})


def deploy_shape(normalized_query: str) -> bool:
    """"deploy the app to production", "ship this to staging"."""
    rest = _verb_then(command_words(normalized_query), _DEPLOY_VERBS, _DEPLOY_PHRASAL_VERBS)
    return rest is not None and bool(_DEPLOY_TARGETS & set(rest))


# --- issue filing: a filing verb, a defect, and GitHub ---------------------
_FILING_VERBS = frozenset({"file", "open", "log", "create", "raise", "report", "submit", "write"})
_DEFECT_WORDS = frozenset({"bug", "bugs", "issue", "issues", "problem", "problems", "defect", "regression"})


def issue_filing_shape(normalized_query: str) -> bool:
    """"file a bug on GitHub about the upload crash", "raise a github issue for the regression"."""
    rest = _verb_then(command_words(normalized_query), _FILING_VERBS)
    if rest is None:
        return False
    words = set(rest)
    return bool(_DEFECT_WORDS & words) and "github" in words


# --- appearance change: an edit verb on how a UI looks ---------------------
_EDIT_VERBS = frozenset({"change", "restyle", "redesign", "update", "adjust", "tweak", "rework"})
_APPEARANCE_NOUNS = frozenset(
    {"style", "styles", "styling", "theme", "color", "colors", "colour", "font", "fonts", "layout", "css", "spacing"}
)
_UI_SURFACE_WORDS = frozenset(
    {"page", "pages", "screen", "button", "header", "footer", "navbar", "sidebar", "form", "modal", "ui", "site", "website", "app", "component"}
)


def appearance_change_shape(normalized_query: str) -> bool:
    """An edit verb, an appearance noun, and a UI surface: "change the login page style"."""
    rest = _verb_then(command_words(normalized_query), _EDIT_VERBS)
    if rest is None:
        return False
    words = set(rest)
    return bool(_APPEARANCE_NOUNS & words) and bool(_UI_SURFACE_WORDS & words)


__all__ = [
    "appearance_change_shape",
    "build_failure_shape",
    "code_review_shape",
    "command_words",
    "deploy_shape",
    "issue_filing_shape",
]
