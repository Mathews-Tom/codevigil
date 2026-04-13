"""Shared phrase-matching helper for text-scanning collectors.

Both :mod:`codevigil.collectors.stop_phrase` and
:mod:`codevigil.collectors.reasoning_loop` need to scan assistant message
text for a configurable list of phrases, with three matching modes:

* ``word``    - word-boundary match (default), the safe form that prevents
                ``"should I"`` from firing on ``"shoulder"``.
* ``regex``   - caller-supplied regex pattern.
* ``substring`` - naive substring containment, opt-in only.

When the total phrase count grows past 32 the helper transparently
escalates from a single compiled regex (an alternation) to an
Aho-Corasick automaton (only for the ``word`` and ``substring`` subset of
phrases - ``regex`` phrases always go through :mod:`re`). Aho-Corasick
gives O(L + matches) per message instead of the naive O(P*L), which is
the documented escape hatch in the design's *Complexity Honesty* table.

The automaton is implemented inline (trie + failure links) so we avoid
introducing any optional third-party dependency, in line with the
stdlib-only privacy gate.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

PhraseMode = Literal["word", "regex", "substring"]

# Once the total phrase table grows past this many entries the matcher
# escalates from a compiled regex alternation to a hand-rolled
# Aho-Corasick automaton. Documented in design.md section Complexity Honesty.
_AC_ESCALATION_THRESHOLD: int = 32


@dataclass(frozen=True, slots=True)
class PhraseSpec:
    """One configured phrase entry.

    ``intent`` is documentation only - it surfaces in ``--explain`` output
    so users can audit why a particular phrase is in the table.
    """

    text: str
    mode: PhraseMode = "word"
    category: str = "default"
    intent: str | None = None


@dataclass(frozen=True, slots=True)
class PhraseMatch:
    """One match emitted by :meth:`Matcher.match`."""

    spec: PhraseSpec
    start: int
    end: int
    matched: str


# ---------------------------------------------------------------------------
# Matcher implementations
# ---------------------------------------------------------------------------


class _RegexMatcher:
    """Naive matcher: one compiled regex per phrase, all run sequentially.

    O(P*L) per scanned text. Used for small phrase tables and as the
    fallback for phrases whose mode is ``"regex"`` (Aho-Corasick can only
    accept literal patterns).
    """

    def __init__(self, specs: list[PhraseSpec]) -> None:
        self._compiled: list[tuple[PhraseSpec, re.Pattern[str]]] = [
            (spec, _compile_pattern(spec)) for spec in specs
        ]

    def match(self, text: str) -> Iterator[PhraseMatch]:
        for spec, pattern in self._compiled:
            for hit in pattern.finditer(text):
                yield PhraseMatch(
                    spec=spec,
                    start=hit.start(),
                    end=hit.end(),
                    matched=hit.group(0),
                )


class _AhoCorasickMatcher:
    """Aho-Corasick automaton over the literal-mode phrase subset.

    Handles ``word`` and ``substring`` phrases (case-insensitive). Any
    ``regex`` phrases are handed off to a small ``_RegexMatcher`` and the
    two streams of matches are interleaved in document order.

    Implementation: a trie keyed on lowercased characters, plus failure
    links computed via BFS (the textbook construction). Output edges
    carry the original :class:`PhraseSpec` so we can preserve mode
    information when post-filtering for word boundaries.
    """

    __slots__ = ("_fail", "_goto", "_output", "_regex_fallback")

    def __init__(self, specs: list[PhraseSpec]) -> None:
        literal_specs = [s for s in specs if s.mode in {"word", "substring"}]
        regex_specs = [s for s in specs if s.mode == "regex"]
        self._regex_fallback: _RegexMatcher | None = (
            _RegexMatcher(regex_specs) if regex_specs else None
        )
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._output: list[list[PhraseSpec]] = [[]]
        for spec in literal_specs:
            self._add(spec)
        self._build_failure_links()

    def _add(self, spec: PhraseSpec) -> None:
        node = 0
        for char in spec.text.lower():
            nxt = self._goto[node].get(char)
            if nxt is None:
                nxt = len(self._goto)
                self._goto.append({})
                self._fail.append(0)
                self._output.append([])
                self._goto[node][char] = nxt
            node = nxt
        self._output[node].append(spec)

    def _build_failure_links(self) -> None:
        queue: deque[int] = deque()
        for nxt in self._goto[0].values():
            self._fail[nxt] = 0
            queue.append(nxt)
        while queue:
            node = queue.popleft()
            for char, nxt in self._goto[node].items():
                queue.append(nxt)
                fail = self._fail[node]
                while fail and char not in self._goto[fail]:
                    fail = self._fail[fail]
                self._fail[nxt] = self._goto[fail].get(char, 0)
                if self._fail[nxt] == nxt:
                    self._fail[nxt] = 0
                self._output[nxt].extend(self._output[self._fail[nxt]])

    def match(self, text: str) -> Iterator[PhraseMatch]:
        # Run the automaton across the lowercased text and emit a
        # PhraseMatch every time an output node is hit. For ``word``-mode
        # specs we re-check the surrounding characters so the boundary
        # invariant matches the regex form exactly.
        lowered = text.lower()
        node = 0
        hits: list[PhraseMatch] = []
        for index, char in enumerate(lowered):
            while node and char not in self._goto[node]:
                node = self._fail[node]
            node = self._goto[node].get(char, 0)
            if not self._output[node]:
                continue
            for spec in self._output[node]:
                end = index + 1
                start = end - len(spec.text)
                if start < 0:
                    continue
                if spec.mode == "word" and not _word_boundary_ok(text, start, end):
                    continue
                hits.append(
                    PhraseMatch(
                        spec=spec,
                        start=start,
                        end=end,
                        matched=text[start:end],
                    )
                )
        if self._regex_fallback is not None:
            hits.extend(self._regex_fallback.match(text))
        hits.sort(key=lambda h: (h.start, h.end))
        yield from hits


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------


class Matcher:
    """Uniform facade over the regex and Aho-Corasick implementations."""

    __slots__ = ("_impl", "mode", "phrase_count")

    def __init__(
        self,
        impl: _RegexMatcher | _AhoCorasickMatcher,
        phrase_count: int,
        mode: Literal["regex", "aho_corasick"],
    ) -> None:
        self._impl = impl
        self.phrase_count = phrase_count
        self.mode: Literal["regex", "aho_corasick"] = mode

    def match(self, text: str) -> Iterator[PhraseMatch]:
        return self._impl.match(text)


def compile_phrase_table(
    phrases: list[PhraseSpec],
    *,
    force_mode: Literal["regex", "aho_corasick"] | None = None,
) -> Matcher:
    """Build a :class:`Matcher` for the given phrase list.

    Picks Aho-Corasick when ``len(phrases) > 32`` unless ``force_mode``
    overrides the choice (the override exists for tests that want to
    cross-check the two implementations on identical input).
    """

    if force_mode == "aho_corasick" or (
        force_mode is None and len(phrases) > _AC_ESCALATION_THRESHOLD
    ):
        impl: _RegexMatcher | _AhoCorasickMatcher = _AhoCorasickMatcher(phrases)
        chosen: Literal["regex", "aho_corasick"] = "aho_corasick"
    else:
        impl = _RegexMatcher(phrases)
        chosen = "regex"
    return Matcher(impl=impl, phrase_count=len(phrases), mode=chosen)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compile_pattern(spec: PhraseSpec) -> re.Pattern[str]:
    if spec.mode == "regex":
        return re.compile(spec.text, re.IGNORECASE)
    if spec.mode == "substring":
        return re.compile(re.escape(spec.text), re.IGNORECASE)
    # Default ``word`` mode: word-boundary anchored on each side. We use
    # the lookaround form ``(?<!\w)...(?!\w)`` rather than ``\b...\b`` so
    # phrases that begin or end with non-word characters (e.g. a trailing
    # comma) still anchor correctly.
    return re.compile(
        r"(?<!\w)" + re.escape(spec.text) + r"(?!\w)",
        re.IGNORECASE,
    )


def _word_boundary_ok(text: str, start: int, end: int) -> bool:
    """Replicate the regex ``(?<!\\w)...(?!\\w)`` check for AC matches."""

    if start > 0 and _is_word_char(text[start - 1]):
        return False
    return not (end < len(text) and _is_word_char(text[end]))


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


__all__ = [
    "Matcher",
    "PhraseMatch",
    "PhraseMode",
    "PhraseSpec",
    "compile_phrase_table",
]
