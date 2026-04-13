"""Aho-Corasick escalation path correctness."""

from __future__ import annotations

from codevigil.collectors._text_match import PhraseSpec, compile_phrase_table


def _phrase(i: int) -> PhraseSpec:
    return PhraseSpec(text=f"phrase_{i:03d}", category="bulk", mode="word")


def test_aho_corasick_escalation_above_threshold() -> None:
    phrases = [_phrase(i) for i in range(50)]
    matcher = compile_phrase_table(phrases)
    assert matcher.mode == "aho_corasick"

    text = "hello phrase_001 world phrase_017 tail phrase_049 end"
    hits = list(matcher.match(text))
    matched_texts = {hit.spec.text for hit in hits}
    assert matched_texts == {"phrase_001", "phrase_017", "phrase_049"}


def test_aho_corasick_word_boundary_filtering() -> None:
    phrases = [_phrase(i) for i in range(40)]
    matcher = compile_phrase_table(phrases)
    # phrase_001 inside an otherwise-word-character run must not fire.
    text = "xphrase_001y is glued; phrase_001 is not."
    hits = list(matcher.match(text))
    assert len(hits) == 1
    assert hits[0].matched == "phrase_001"


def test_force_mode_regex_matches_aho_corasick_results() -> None:
    phrases = [_phrase(i) for i in range(50)]
    text = "leading " + " ".join(f"phrase_{i:03d}" for i in (3, 11, 27, 41)) + " trailing"
    naive = compile_phrase_table(phrases, force_mode="regex")
    fast = compile_phrase_table(phrases, force_mode="aho_corasick")
    naive_hits = sorted(h.matched for h in naive.match(text))
    fast_hits = sorted(h.matched for h in fast.match(text))
    assert naive_hits == fast_hits
    assert naive_hits == ["phrase_003", "phrase_011", "phrase_027", "phrase_041"]


def test_aho_corasick_handles_5kb_message() -> None:
    phrases = [_phrase(i) for i in range(50)]
    matcher = compile_phrase_table(phrases)
    filler = "lorem ipsum dolor sit amet " * 200  # ~5.4 KiB
    text = filler + " phrase_007 " + filler + " phrase_022 " + filler
    hits = sorted(h.spec.text for h in matcher.match(text))
    assert hits == ["phrase_007", "phrase_022"]
