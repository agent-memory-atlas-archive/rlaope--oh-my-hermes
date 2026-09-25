"""Rank the catalog lexically so an undecided route still names the right skill.

The scorer in `recommend.py` answers "which skill's trigger table does this
message hit hardest". That is the right question for a dispatch and the wrong
one for a shortlist: a user who does not know a skill's vocabulary writes the
situation instead ("keep a note that our API uses snake_case"), no trigger fires,
and the clarify that follows offers whatever the everyday words happened to
touch.

This module ranks the same catalog a second way, as a bag of words: BM25 over
each skill's name, English triggers, description, `use_when`, and the
`situations` field, which exists for exactly this reader -- the plain phrases a
user in that situation would type. It has two readers. `candidate_handoff`
fills an undecided route's shortlist from it, after the route is already a
clarify; there it never reads a scorer result and never changes an action, so
a lexical accident can put a skill on a shortlist Hermes chooses from and
cannot dispatch it. `jev_addressing` uses it to pick a Jev sibling for a
message already addressed to Jev, and only on an absolute score floor and a
clear lead over the next sibling; below either it keeps `jev-ask`.

Ranking is not admission. `candidate_handoff` admits a ranked skill only when
the message shares an anchor word with it (`lexical_anchor_terms`) and the
skill's own offers-itself precondition holds, so the shortlist inherits the
word-sense exclusions the scorer already learned instead of relearning them.

Deterministic and stdlib only: the index is built once from catalog data and
the same message always yields the same ranking.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import math

from .localization import routing_terms
from .recommend import held_back_trigger_tokens
from ..skills.catalog import routable_definitions
from ..skills.catalog_types import SkillDefinition


# Field weights: a word in a skill's name says the most about it, trigger and
# situation words were written as things a user says, and description and
# use_when words are prose about the skill.
FIELD_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("name", 3.0),
    ("triggers", 2.0),
    ("situations", 2.0),
    ("description", 1.0),
    ("use_when", 1.0),
)

# BM25 constants, the textbook defaults.
_K1 = 1.2
_B = 0.75

# Function words, pronouns, and request filler. A token here carries no
# information about which skill a message wants, only about English grammar or
# politeness, so it is dropped from both the index and the query. Domain-bearing
# words ("review", "memory", "deploy") are never listed.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an the this that these those our my your its it i we you me us them they he she him her
    to of for in on at by with from into onto about as and or but if then so than too very
    is are was were be been being am do does did done doing have has had having
    can could should would will shall may might must not no yes
    what which who whom whose when where why how all any some each every both either neither
    just also more most less much many here there please let lets make get got go going want
    need needs help tell give show see look like thing things something anything someone
    stuff it's i'm i've i'd we're we've don't can't won't isn't doesn't didn't there's that's
    omh skill skills workflow workflows use used using uses while again once now then
    one two up down out over under off only own same other such via per etc e.g i.e
    """.split()
)


# Words whose final s or es is not an inflection.
_STEM_EXCEPTIONS = frozenset({"series", "species", "news", "does", "goes", "yes", "this", "thus", "bus", "gas", "lens", "atlas"})


def stem(token: str) -> str:
    """Fold common English inflections so a word's forms meet; the stem need not be a word.

    Rules, in order:
    1. exceptions stay as they are (`series`, `news`, `does`, `goes`);
    2. `-ies` -> `-y` (`queries` -> `query`);
    3. `-es` after s/x/z/ch/sh is stripped (`caches` -> `cach`, `fixes` -> `fix`);
    4. a plain `-s` is stripped unless the word ends in -ss, -us, or -is
       (`pages` -> `page`, `class`, `status`, `analysis` stay);
    5. `-ing` / `-ed` are stripped, with a doubled final consonant undone
       (`running` -> `run`, `failed` -> `fail`);
    6. a final silent `e` is dropped from words longer than four letters, so
       `cache`/`caches`, `style`/`styling`, `service`/`services`,
       `issue`/`issues` meet, while short words (`note`, `page`) keep it.
    A light stemmer, not a lemmatizer: `make` and `making` still differ.
    """
    if token in _STEM_EXCEPTIONS:
        return token
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("sses", "xes", "zes", "ches", "shes")):
        token = token[:-2]
    elif len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    for suffix in ("ing", "ed"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            base = token[: -len(suffix)]
            if len(base) > 2 and base[-1] == base[-2] and base[-1] not in "lsz":
                base = base[:-1]
            token = base
            break
    if len(token) > 4 and token.endswith("e") and not token.endswith("ee"):
        token = token[:-1]
    return token


def lexical_terms(text: str) -> list[str]:
    """ASCII content words of `text`, stopworded and stemmed, in order."""
    terms: list[str] = []
    for raw in sorted(routing_terms(text)):
        for part in raw.split("-"):
            if len(part) < 2 or not part.isascii() or not part.isalnum() or part in STOPWORDS:
                continue
            terms.append(stem(part))
    return terms


def _field_text(definition: SkillDefinition, field: str) -> str:
    if field == "name":
        return definition.name
    if field == "triggers":
        return " ".join(trigger for trigger in definition.triggers if trigger.isascii())
    if field == "situations":
        return " ".join(definition.situations)
    if field == "description":
        return definition.description.replace("[omh]", " ")
    return definition.use_when


@dataclass(frozen=True)
class _LexicalIndex:
    documents: tuple[tuple[str, dict[str, float], float], ...]
    idf: dict[str, float]
    average_length: float


def _document(definition: SkillDefinition) -> tuple[dict[str, float], float]:
    weights: Counter[str] = Counter()
    length = 0.0
    for field, weight in FIELD_WEIGHTS:
        # A word counts once per field, so a long description that repeats a
        # word does not outweigh a name that states it.
        for term in set(lexical_terms(_field_text(definition, field))):
            weights[term] += weight
            length += weight
    return dict(weights), length


@lru_cache(maxsize=1)
def _index() -> _LexicalIndex:
    documents = []
    document_frequency: Counter[str] = Counter()
    for definition in routable_definitions():
        weights, length = _document(definition)
        documents.append((definition.name, weights, length))
        document_frequency.update(weights.keys())
    count = len(documents)
    idf = {
        term: math.log((count - frequency + 0.5) / (frequency + 0.5) + 1.0)
        for term, frequency in document_frequency.items()
    }
    average_length = sum(length for _, _, length in documents) / max(count, 1)
    return _LexicalIndex(documents=tuple(documents), idf=idf, average_length=average_length)


@lru_cache(maxsize=4096)
def lexical_ranking(message: str, drop_terms: frozenset[str] = frozenset()) -> tuple[tuple[str, float], ...]:
    """Every catalog skill that shares a content word with `message`, best first.

    `drop_terms` leaves words out of the query -- an addressee's name, say, that
    every candidate shares and that therefore says nothing about which one is
    meant. Ties break on the skill name so the order is reproducible.
    """
    index = _index()
    query = set(lexical_terms(message)) - drop_terms
    if not query:
        return ()
    scored: list[tuple[str, float]] = []
    for name, weights, length in index.documents:
        norm = _K1 * (1.0 - _B + _B * length / index.average_length)
        score = 0.0
        for term in sorted(query):
            frequency = weights.get(term)
            if frequency:
                score += index.idf[term] * frequency * (_K1 + 1.0) / (frequency + norm)
        if score > 0.0:
            scored.append((name, round(score, 6)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return tuple(scored)


@lru_cache(maxsize=None)
def _anchor_vocabulary(skill: str) -> frozenset[str]:
    """Words a user says about `skill`: its name, situations, and English triggers.

    Minus the tokens the router already credits to it only inside a whole
    phrase (`_WHOLE_PHRASE_ONLY_TRIGGER_TOKENS` and the language-pack
    holdbacks). Those are the words the scorer learned are ambiguous for this
    skill -- `document` for long-document-reading, `close` for
    finance-analysis -- and a word the scorer refuses to count alone must not
    admit the skill to a shortlist alone either.
    """
    definition = next((item for item in routable_definitions() if item.name == skill), None)
    if definition is None:
        return frozenset()
    said = set(lexical_terms(_field_text(definition, "situations")))
    said.update(lexical_terms(_field_text(definition, "triggers")))
    said.update(lexical_terms(_field_text(definition, "name")))
    held_back = {stem(token) for token in held_back_trigger_tokens(skill)}
    return frozenset(said - held_back)


# A word more than this many skills' catalog text uses is catalog-common:
# `test`, `page`, `release`, `fail` describe a dozen skills each, so sharing one
# says nothing about which skill a message wants.
ANCHOR_MAX_DOCUMENT_FREQUENCY = 8
# Below this BM25 score a ranked skill shares too little with the message to
# be offered at all, anchored or not.
LEXICAL_SCORE_FLOOR = 5.0


@lru_cache(maxsize=1)
def _document_frequency() -> dict[str, int]:
    frequency: Counter[str] = Counter()
    for _name, weights, _length in _index().documents:
        frequency.update(weights.keys())
    return dict(frequency)


def only_held_back_overlap(message: str, skill: str) -> bool:
    """Every word `message` shares with `skill` is one the router holds back for it."""
    index = {name: weights for name, weights, _length in _index().documents}
    shared = frozenset(lexical_terms(message)) & frozenset(index.get(skill, {}))
    held_back = {stem(token) for token in held_back_trigger_tokens(skill)}
    return bool(shared) and shared <= held_back


def lexical_anchor_terms(message: str, skill: str) -> frozenset[str]:
    """The message's words that anchor `skill`: shared with what users say about it.

    A skill that shares only description prose with a message ranks, but it
    is not a shortlist entry: prose words ("interface", "session", "report")
    describe many skills, and the situations and triggers are where a skill
    says which requests are its own. A catalog-common word is not an anchor
    either, wherever it appears.
    """
    frequency = _document_frequency()
    return frozenset(
        term
        for term in frozenset(lexical_terms(message)) & _anchor_vocabulary(skill)
        if frequency.get(term, 0) <= ANCHOR_MAX_DOCUMENT_FREQUENCY
    )


__all__ = [
    "ANCHOR_MAX_DOCUMENT_FREQUENCY",
    "FIELD_WEIGHTS",
    "LEXICAL_SCORE_FLOOR",
    "STOPWORDS",
    "lexical_anchor_terms",
    "lexical_ranking",
    "lexical_terms",
    "only_held_back_overlap",
    "stem",
]
