"""Keyword matching — plain, literal, and impossible to make pathological.

There is deliberately NO regular-expression engine here. Matching is exact
literal containment, which is linear-time and cannot catastrophically backtrack,
hang, or be exploited (ReDoS). That removes an entire class of bug at the root:
the matcher takes the same form whether the input is 10 bytes or 10 MB.

Each keyword carries a ``mode``:

  * ``contains`` — case-insensitive substring. Matches inside larger words too,
    which is what you want for prefixes like ``CVE-``.
  * ``word`` — case-insensitive whole-word(s) match: the term must be bounded by
    a non-alphanumeric character (or the string edge) on both ends. Avoids the
    classic false positives (``ai`` matching "em**ai**l"). Works for multi-word
    terms too, since boundaries are only checked at the term's outer edges, so
    ``data breach`` matches "a data breach." but not "metadata breachy".

For every entry the title is checked first; only if no keyword matches the title
do we check the body.
"""
from __future__ import annotations

MATCH_MODES: tuple[str, ...] = ("contains", "word")
DEFAULT_MODE = "word"


def normalize_mode(mode: str | None) -> str:
    """Coerce an arbitrary stored value into a known mode (defensive)."""
    return mode if mode in MATCH_MODES else DEFAULT_MODE


def _whole_word_contains(haystack: str, needle: str) -> bool:
    """True if ``needle`` occurs in ``haystack`` bounded by non-alphanumerics.

    Both arguments must already be lower-cased. Pure scan, no regex — O(n*m) in
    the worst case but with no backtracking blowup whatsoever.
    """
    if not needle:
        return False
    start = 0
    n = len(needle)
    hlen = len(haystack)
    while True:
        i = haystack.find(needle, start)
        if i == -1:
            return False
        before_ok = i == 0 or not haystack[i - 1].isalnum()
        after_idx = i + n
        after_ok = after_idx >= hlen or not haystack[after_idx].isalnum()
        if before_ok and after_ok:
            return True
        start = i + 1


def text_matches(term: str, mode: str, text: str) -> bool:
    """Does ``term`` (in the given mode) occur in ``text``? Case-insensitive."""
    if not term or not text:
        return False
    t = text.lower()
    needle = term.lower()
    if normalize_mode(mode) == "word":
        return _whole_word_contains(t, needle)
    return needle in t


def entry_matches(term: str, mode: str, title: str, body: str) -> bool:
    """Title-first matching for one (term, mode) against one entry."""
    if text_matches(term, mode, title or ""):
        return True
    return text_matches(term, mode, body or "")
