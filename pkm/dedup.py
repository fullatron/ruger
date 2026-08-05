"""D4: one task, with a mention count.

Matching is same `owner`, plus normalised-text similarity above a threshold,
among open rows only. Token-overlap Jaccard to start; escalate to an LLM
tie-break only for candidate pairs if this misclassifies. Not yet — this is
deliberately the dumb version.
"""

from __future__ import annotations

import re
import unicodedata

from . import config

# Small and boring on purpose. Anything that carries task meaning stays.
STOPWORDS = frozenset(
    """
a an and are as at be been being by do does for from get go going had has have
in into is it its of on onto or our out over per so than that the their them
then there these they this those to up upon us was were will with would you
your i me my we he she his her hers him am can could should shall may might
about again also just make made need needs new next now once other own same
some such very want wants ll ve re s t d m
""".split()
)

_WS = re.compile(r"\s+")


def _depunctuate(text: str) -> str:
    """Replace punctuation with spaces, keeping every letter and its marks.

    `[^\\w\\s]` looks script-agnostic and is not: `\\w` does not match combining
    marks, so every Devanagari vowel sign counts as punctuation. "मैं डेक भेजूंगा"
    came out as "म ड क भ ज ग" — the consonant skeleton — and two unrelated Hindi
    tasks then scored 0.86 and were merged into one.

    Category `M*` is what carries the vowel in most Indic scripts, in Thai, in
    Arabic diacritics and in Hangul. It has to survive.
    """
    return "".join(
        ch if (ch.isalnum() or ch.isspace() or unicodedata.category(ch).startswith("M"))
        else " "
        for ch in text
    )


def normalise_text(text: str) -> str:
    """Lowercase, strip punctuation and stopwords, return a token string.

    NFC rather than NFKD: decomposing splits a letter from its marks, and the
    stripping above then throws the marks away. The cost is that "café" and
    "cafe" no longer fold together, which is a far smaller loss than flattening
    every Indic language to its consonants.
    """
    text = unicodedata.normalize("NFC", text or "").casefold()
    text = _depunctuate(text)
    tokens = [t for t in _WS.split(text) if t and t not in STOPWORDS]
    return " ".join(tokens)


def tokens(normalised: str) -> set[str]:
    return set(normalised.split())


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def normalise_owner(owner: str, direction: str | None = None) -> str:
    """Fold the user's own names (and first person) to the single key 'me'.

    §5 wants speaker attribution from Granola's mic-vs-system labelling, but an
    exported *summary* has no mic labels — so the user's names are configured
    (PKM_ME) and folded here instead.
    """
    if direction == "mine":
        return "me"

    # Same treatment as the task text: a name written in Devanagari must not be
    # reduced to its consonants before it is compared with PKM_ME.
    raw = unicodedata.normalize("NFC", owner or "").casefold().strip()
    raw = _WS.sub(" ", _depunctuate(raw)).strip()
    if not raw:
        return ""

    aliases = {a.casefold().strip() for a in config.ME_ALIASES}
    aliases |= {"me", "i", "myself", "self"}
    if raw in aliases:
        return "me"
    # "alex" matches a configured alias "Alex Rao", and vice versa.
    for alias in aliases:
        if alias and (raw == alias or raw in alias.split() or alias in raw.split()):
            return "me"
    return raw


def numbers(normalised: str) -> set[str]:
    """Tokens carrying digits: the part of a task that identifies *which* one."""
    return {t for t in normalised.split() if any(c.isdigit() for c in t)}


def distinguishable(a: str, b: str) -> bool:
    """True when two texts name different things despite similar wording.

    "Send invoice 1041" and "Send invoice 1042" share every word but one and
    score 0.75, comfortably over the threshold — so dedup merged them and one
    invoice silently vanished. Numbers are usually the whole point of the task.

    Only applies when BOTH carry digits. "Send the report by 5pm" against "send
    the report" is one task described twice, and must still merge.
    """
    na, nb = numbers(a), numbers(b)
    return bool(na) and bool(nb) and na != nb


def find_match(
    candidate: dict,
    existing: list,
    threshold: float | None = None,
) -> tuple[object | None, float]:
    """Best open commitment this candidate is a restatement of.

    `existing` is rows/dicts that already passed the owner filter. Returns
    (row, score) or (None, best_score) when nothing clears the threshold.
    """
    cutoff = config.DEDUP_THRESHOLD if threshold is None else threshold
    best, best_score = None, 0.0

    for row in existing:
        if distinguishable(candidate["task_norm"], row["task_norm"]):
            continue
        score = jaccard(candidate["task_norm"], row["task_norm"])
        if score > best_score:
            best, best_score = row, score

    if best_score >= cutoff:
        return best, best_score
    return None, best_score
