"""The tiered old_text matcher: it forgives copy slips and never guesses.

Two properties matter more than any single case: a refusal changes nothing,
and an accepted match is the only plausible target. The "must fail" cases are
therefore as important as the "should succeed" ones.
"""

import random
import time
import unicodedata

import pytest

from backend.services.text_matching import (
    FUZZY_MIN_LENGTH,
    _disjoint,
    _edits_are_safe,
    _find_whole,
    _full_normalized_view,
    best_end_distances,
    inline_exact_zones,
    match_text,
    max_fuzzy_distance,
    normalized_view,
    protected_spans,
)

pytestmark = pytest.mark.unit


def _brute_force_best_end(pattern: str, text: str) -> list[int]:
    out = []
    for end in range(1, len(text) + 1):
        best = len(pattern)
        for start in range(0, end + 1):
            best = min(best, _levenshtein(pattern, text[start:end]))
        out.append(best)
    return out


def _levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_a != char_b),
                )
            )
        previous = current
    return previous[-1]


def test_bit_parallel_search_matches_the_textbook_definition():
    generator = random.Random(11)
    for _ in range(300):
        pattern = "".join(
            generator.choice("abc") for _ in range(generator.randint(1, 9))
        )
        text = "".join(generator.choice("abc") for _ in range(generator.randint(0, 14)))
        assert best_end_distances(pattern, text) == _brute_force_best_end(pattern, text)


def _safe(scope: str, old: str, *, occurrences: int = 1, fuzzy: bool = True, **kwargs):
    return match_text(
        scope,
        old,
        expected_occurrences=occurrences,
        mode="safe",
        allow_fuzzy=fuzzy,
        protected=protected_spans(scope),
        **kwargs,
    )


def _applied(scope: str, outcome, new: str) -> str:
    assert outcome.status == "matched", outcome
    parts, cursor = [], 0
    for start, end in outcome.spans:
        parts += [scope[cursor:start], new]
        cursor = end
    return "".join(parts) + scope[cursor:]


NBSP = chr(0xA0)  # written as a code point: it is invisible
SENTENCE = "The quick brown fox jumps over the lazy dog."


def test_exact_is_used_first_and_counts_must_agree():
    outcome = _safe(SENTENCE, "brown fox")
    assert (outcome.status, outcome.kind) == ("matched", "exact")
    doubled = _safe("a fox. a fox.", "a fox")
    assert doubled.status == "count_mismatch"
    assert len(doubled.candidates) == 2


@pytest.mark.parametrize(
    ("scope", "old"),
    [
        ("line one\r\nline two", "line one\nline two"),
        ("단어  사이   공백이 넓다", "단어 사이 공백이 넓다"),
        # One trailing space renders as nothing (two or more before a
        # non-blank line would be a hard break, which is not invisible).
        ("trailing space \nnext", "trailing space\nnext"),
        ("It’s “quoted” — fine", 'It\'s "quoted" - fine'),
        ("no" + NBSP + "break space here", "no break space here"),
        (unicodedata.normalize("NFD", "한국어 문장입니다"), "한국어 문장입니다"),
        ("한국어 문장입니다", unicodedata.normalize("NFD", "한국어 문장입니다")),
    ],
)
def test_invisible_differences_match_through_the_normalized_view(scope, old):
    outcome = _safe(scope, old, fuzzy=False)
    assert (outcome.status, outcome.kind) == ("matched", "normalized")
    start, _end = outcome.spans[0]
    # The span is in ORIGINAL coordinates.
    assert start == 0


def test_normalization_keeps_an_exact_map_back_to_the_original():
    scope = "a" + NBSP + " \tb\r\nc" + unicodedata.normalize("NFD", "한")
    view = normalized_view(scope)
    assert view.text == "a b\nc한"
    assert view.original_span(1, 2) == (1, 4)  # the whole whitespace run
    assert view.original_span(5, 6) == (len(scope) - 3, len(scope))


@pytest.mark.parametrize(
    "typo",
    [
        "The quick brown fox jump over the lazy dog.",  # dropped letter
        "The quick brown fox jumpss over the lazy dog.",  # doubled letter
        "The quick brwon fox jumps over the lazy dog.",  # transposition (2 edits)
    ],
)
def test_a_single_copy_slip_inside_a_named_block_is_forgiven(typo):
    scope = "Intro line.\n\n" + SENTENCE + "\n\nOutro line here."
    outcome = _safe(scope, typo)
    if typo == "The quick brwon fox jumps over the lazy dog.":
        # 44 chars allows one edit; a transposition costs two -> refused.
        assert outcome.status == "low_confidence"
        return
    assert (outcome.status, outcome.kind) == ("matched", "fuzzy")
    assert _applied(scope, outcome, "REPLACED") == (
        "Intro line.\n\nREPLACED\n\nOutro line here."
    )


def test_a_korean_particle_slip_is_forgiven_in_a_long_enough_sentence():
    scope = "- 결정: 항목 3은 유지하고 근거를 다시 정리합니다\n- 근거: 측정값 21ms"
    outcome = _safe(scope, "결정: 항목 3는 유지하고 근거를 다시 정리합니다")
    assert (outcome.status, outcome.kind) == ("matched", "fuzzy")


def test_fuzzy_never_runs_without_a_named_scope():
    outcome = _safe(
        SENTENCE, "The quick brown fox jump over the lazy dog.", fuzzy=False
    )
    assert outcome.status == "not_found"
    assert outcome.fuzzy_skipped
    # ...but it still says where the nearest text is, without applying it.
    assert outcome.candidates and outcome.candidates[0].start == 0


def test_short_old_text_must_match_exactly():
    assert max_fuzzy_distance(FUZZY_MIN_LENGTH - 1) == 0
    short = "tha lazy do"  # 11 characters, one slip from "the lazy do"
    assert len(short) == FUZZY_MIN_LENGTH - 1
    outcome = _safe("the lazy dog", short)
    assert outcome.status == "not_found"


def test_one_slip_must_stay_a_small_share_of_old_text():
    # 20 characters with one slip is 95% similar: below the 96% floor for
    # old_text under 32 characters, so it is refused rather than applied.
    scope = "Intro.\n\nrestart every service\n\nOutro."
    old = "restart evry service"
    assert len(old) == 20
    outcome = _safe(scope, old)
    assert outcome.status == "low_confidence"
    assert "share" in outcome.reason
    # 26 characters with one slip is 96.2% similar: accepted.
    longer = "Intro.\n\nrestart every service today\n\nOutro."
    assert _safe(longer, "restart evry service today").status == "matched"


def test_the_same_sentence_twice_is_ambiguous_not_guessed():
    scope = SENTENCE + "\n\n" + SENTENCE
    outcome = _safe(scope, "The quick brown fox jump over the lazy dog.")
    assert outcome.status == "ambiguous"
    assert len(outcome.candidates) == 2


def test_a_near_runner_up_blocks_the_match():
    scope = (
        "The quick brown fox jumps over the lazy dog.\n\n"
        "The quick brown fox leaps over the lazy dog."
    )
    # One edit from the first, a few more from the second: the margin rule
    # needs the runner-up at least two edits further away.
    outcome = _safe(scope, "The quick brown fox jump over the lazy dog.")
    assert outcome.status == "matched"
    close_rival = SENTENCE + "\n\nThe quick brown fox jumpx over the lazy dog."
    assert _safe(
        close_rival, "The quick brown fox jumpz over the lazy dog."
    ).status == ("ambiguous")


def test_identifiers_are_never_fuzzily_rewritten():
    outcome = _safe(
        "call parse_block_map here to rebuild", "call parse_block_mop here to rebuild"
    )
    assert outcome.status in {"low_confidence", "not_found"}, outcome


def test_code_blocks_are_protected_zones():
    scope = "Text before.\n\n```py\nvalue  =  compute(items)\n```\n"
    code = protected_spans(scope, block_kinds=(("code", 14, len(scope)),))
    outcome = match_text(
        scope,
        "value = compute(items)",
        expected_occurrences=1,
        mode="safe",
        allow_fuzzy=True,
        protected=code,
    )
    # Collapsing the spaces would be a normalization inside code: refused.
    assert outcome.status != "matched"


def test_exact_mode_never_normalizes():
    outcome = match_text(
        "a  b c d e f g h i j k l",
        "a b c d e f g h i j k l",
        expected_occurrences=1,
        mode="exact",
        allow_fuzzy=True,
    )
    assert outcome.status == "not_found"


def test_multiple_occurrences_are_never_fuzzy():
    outcome = _safe(
        SENTENCE, "The quick brown fox jump over the lazy dog.", occurrences=2
    )
    assert outcome.status == "not_found"


def test_fuzzy_cost_is_bounded_on_a_large_repetitive_block():
    scope = ("lorem ipsum dolor sit amet consectetur " * 1500).strip()
    pattern = (
        ("lorem ipsum dolor sit amet consectetur " * 12)
        .strip()
        .replace("dolor", "dolr", 1)
    )
    started = time.perf_counter()
    outcome = _safe(scope, pattern)
    assert time.perf_counter() - started < 5
    assert outcome.status in {"ambiguous", "not_found", "low_confidence"}


def test_fuzzy_is_skipped_past_the_size_bound():
    scope = "word " * 20_000
    outcome = _safe(scope, "The quick brown fox jump over the lazy dog.")
    assert outcome.status == "not_found"
    assert outcome.fuzzy_skipped


def test_an_equally_close_span_keeps_the_punctuation_old_text_ends_with():
    # "...log tea." is one edit from "...log team" and from "...log team.";
    # taking the shorter span would leave the period behind new_text.
    scope = "Intro line here.\n\nresult install today log team.\n\nOutro."
    outcome = _safe(scope, "result install today log tea.")
    assert (outcome.status, outcome.kind) == ("matched", "fuzzy")
    assert _applied(scope, outcome, "NEW.") == "Intro line here.\n\nNEW.\n\nOutro."


ELLIPSIS = chr(0x2026)
COMBINING_ACUTE = chr(0x301)


def test_an_expanded_character_matches_as_a_whole():
    # The normalized view writes an ellipsis as "...": old_text that spells it
    # that way matches the one original character.
    scope = "wait" + ELLIPSIS + " more text here"
    whole = _safe(scope, "wait...", fuzzy=False)
    assert (whole.status, whole.kind) == ("matched", "normalized")
    assert _applied(scope, whole, "stop.") == "stop. more text here"


def test_part_of_an_expanded_character_is_not_a_whole_unit():
    # Each "." of an ellipsis's "..." maps back to the SAME character, so a
    # match over one of them would splice new_text in once per dot. The
    # unit check refuses it on its own; _disjoint (below) only backs it up,
    # which is why a whole-match test could not tell the two apart.
    view = normalized_view("wait" + ELLIPSIS + " more")
    assert view.text.startswith("wait... ")
    assert not view.covers_whole_units(4, 5)
    assert not view.covers_whole_units(5, 7)
    assert view.covers_whole_units(4, 7)


def test_matches_mapping_to_overlapping_text_are_never_disjoint():
    # The fail-closed check behind the unit rule: spans that share original
    # text can never both be replaced.
    assert _disjoint([(0, 2), (2, 4), (6, 9)])
    assert not _disjoint([(4, 5), (4, 5)])
    assert not _disjoint([(0, 3), (2, 4)])


def test_exact_matching_never_splits_a_decomposed_character():
    # "cafe" is a literal prefix of NFD "café"; replacing it would leave the
    # accent to land on whatever new_text ends with.
    scope = "I like cafe" + COMBINING_ACUTE + " today"
    outcome = _safe(scope, "cafe")
    assert outcome.status == "not_found"
    # The whole character matches, whichever form old_text uses.
    for old in ("caf" + chr(0xE9), "cafe" + COMBINING_ACUTE):
        found = _safe(scope, old)
        assert found.status == "matched", old
        assert _applied(scope, found, "tea") == "I like tea today"


@pytest.mark.parametrize(
    ("scope", "old"),
    [
        # A ZWJ sequence (woman + ZWJ + laptop) is one displayed character.
        ("Role " + chr(0x1F469) + chr(0x200D) + chr(0x1F4BB) + " ready.", chr(0x1F469)),
        # A skin-tone modifier belongs to the emoji before it.
        ("Wave " + chr(0x1F44B) + chr(0x1F3FD) + " hello", chr(0x1F44B)),
        # Two regional indicators make one flag: splitting it changes the flag.
        ("Flag " + chr(0x1F1F0) + chr(0x1F1F7) + " here", chr(0x1F1F0)),
    ],
)
def test_exact_matching_never_splits_an_emoji_sequence(scope, old):
    assert _safe(scope, old).status == "not_found"


def test_flags_pair_left_to_right():
    # Three regional indicators: the first two are one flag, the third starts
    # another. Old_text naming the second flag's lone indicator is whole.
    k, r, j = chr(0x1F1F0), chr(0x1F1F7), chr(0x1F1EF)
    scope = "x " + k + r + j + " y"
    outcome = _safe(scope, j)
    assert outcome.status == "matched"
    assert outcome.spans == ((4, 5),)


def test_disjoint_or_shifted_rivals_make_a_slip_ambiguous():
    # Two places are within the margin: the first five words and the
    # "alpha alpha alpha alpha; alpha" run after it.
    scope = "xlpha alpha alpha alpha alpha; alpha alpha alpha alpha alxxa"
    assert _safe(scope, "alpha alpha alpha alpha alpha").status == "ambiguous"
    # Overlapping but shifted by a word: words 1-5 and 2-6 fit equally well.
    six = "alpha alpha alpha alpha alpha alpha"
    assert _safe(six, "alpha alpha alphx alpha alpha").status == "ambiguous"


def test_a_line_break_is_never_corrected_by_the_fuzzy_tier():
    first = "The first paragraph explains the deployment order in some detail here."
    second = "The second paragraph lists the rollback steps operators must follow."
    scope = first + "\n\n" + second
    outcome = _safe(scope, first + " " + second)
    assert outcome.status == "low_confidence"
    assert "line break" in outcome.reason
    # Stating the break explicitly still matches both paragraphs exactly.
    assert _safe(scope, first + "\n\n" + second).status == "matched"


def test_the_shortcut_view_is_identical_to_the_per_character_one():
    """A scope that normalizes to itself skips the per-character pass (it
    cost ~1s and ~50MB per operation on a 1MB page). The shortcut must agree
    with the full pass exactly -- same text, same mapping, same unit edges."""

    pieces = [
        "plain",
        " ",
        "  ",
        "\n",
        "\r\n",
        "\t",
        NBSP,
        "결정",
        unicodedata.normalize("NFD", "한글"),
        "e" + chr(0x301),
        ELLIPSIS,
        "“q”",
        "—",
        chr(0x1F469) + chr(0x200D) + chr(0x1F4BB),
        chr(0x1F1F0) + chr(0x1F1F7),
        " \n",
    ]
    generator = random.Random(5)
    shortcuts = 0
    for _ in range(400):
        text = "".join(
            generator.choice(pieces) for _ in range(generator.randint(1, 10))
        )
        fast, full = normalized_view(text), _full_normalized_view(text)
        assert fast.text == full.text, repr(text)
        if fast.identity:
            shortcuts += 1
            assert fast.text == text
        for start in range(len(fast.text)):
            for end in range(start + 1, min(len(fast.text), start + 4) + 1):
                assert fast.original_span(start, end) == full.original_span(
                    start, end
                ), (repr(text), start, end)
                assert fast.covers_whole_units(start, end) == full.covers_whole_units(
                    start, end
                ), (repr(text), start, end)
    assert shortcuts > 5, "the shortcut must actually be exercised"
    # The cases the shortcut exists for: ordinary prose, ASCII or Korean.
    for plain in (
        "The quick brown fox jumps over the lazy dog.",
        "결정 사항은 다음과 같다\n항목을 유지한다",
        "line one\nline two\n",
    ):
        view = normalized_view(plain)
        assert view.identity, plain
        # The point of the shortcut: no per-character offset lists at all.
        assert not view.starts and not view.ends
    # And the cases it must not take.
    for shaped in (
        "two  spaces",
        "line\r\nbreak",
        "tab\there",
        "caf" + chr(0x301),
        "wait" + ELLIPSIS,
        "flag " + chr(0x1F1F0) + chr(0x1F1F7),
        "a" + NBSP + "b",
    ):
        assert not normalized_view(shaped).identity, shaped


@pytest.mark.parametrize("text", ["foo\n ", " ", "\n "])
def test_a_last_line_of_only_spaces_never_takes_the_shortcut(text):
    # Such a line is blank and normalizes to nothing even with no line break
    # after it; the shortcut used to keep it.
    assert normalized_view(text).text == _full_normalized_view(text).text
    assert not normalized_view(text).identity


def test_a_line_of_only_spaces_is_a_blank_line():
    # It renders as nothing, so old_text copied without the stray spaces
    # still matches.
    scope = "First paragraph here.\n   \nSecond paragraph here."
    assert normalized_view(scope).text == (
        "First paragraph here.\n\nSecond paragraph here."
    )
    outcome = _safe(scope, "First paragraph here.\n\nSecond paragraph here.")
    assert (outcome.status, outcome.kind) == ("matched", "normalized")


def test_a_rejected_hit_does_not_hide_an_overlapping_real_one():
    # Every start is tried, not only those after the previous hit's end.
    assert _find_whole("aaa", "aa", lambda start, _end: start != 0) == [1]


def test_a_fuzzy_edit_inside_an_exact_only_zone_is_never_safe():
    # Called directly: in a whole match the zone check refuses this too, so
    # only here does a missing check in _edits_are_safe itself show.
    text = "Read the setup guide before every deploy"
    pattern = "Read the setup gide before every deploy"
    zone_start = text.index("setup")
    zone = (zone_start, zone_start + len("setup guide"))
    assert _edits_are_safe(pattern, text, 0, len(text), []) == (True, None)
    safe, why = _edits_are_safe(pattern, text, 0, len(text), [zone])
    assert safe is False
    assert "inside code, a URL or frontmatter" in why


def test_code_copied_exactly_matches_when_the_prose_around_it_does_not():
    # old_text keeps its own code as written, like the page does. Normalized
    # as prose, the two spaces of the code line collapsed and could never
    # match the page's raw code, so the curly quotes elsewhere were fatal.
    scope = "He said “use this”:\n\n```\nx  = 1\n```\n"
    old = 'He said "use this":\n\n```\nx  = 1\n```'
    outcome = _safe(scope, old, fuzzy=False)
    assert (outcome.status, outcome.kind) == ("matched", "normalized")


def test_code_in_old_text_is_compared_where_it_is_not_anywhere_in_it():
    # old_text starts inside the first code span and types curly quotes
    # there; the second span is copied exactly. Checked as a substring, the
    # first span's text was "in old_text" (inside the second) and the match
    # was taken.
    scope = "Run `echo 'hi'` first, then `echo 'hi'` again."
    old = "echo ‘hi’` first, then `echo 'hi'` again."
    outcome = _safe(scope, old, fuzzy=False)
    assert outcome.status == "low_confidence"
    assert "not in old_text exactly" in outcome.reason


def test_a_refusal_names_code_that_differs_instead_of_text_not_found():
    scope = "Run `echo 'hi'` first, then the rest."
    outcome = _safe(scope, "Run `echo ‘hi’` first, then the rest.", fuzzy=False)
    assert outcome.status == "low_confidence"
    assert "not in old_text exactly" in outcome.reason


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_a_table_delimiter_row_is_exact_only_with_any_line_ending(newline):
    # On a CRLF page the row's CR sat before `$`, and on a CR-only page no
    # row after the first started at `^`: the row was then matched loosely.
    text = newline.join(["# T", "", "| Name | Value |", "|:---|---:|", "| a | b |", ""])
    zones = [text[start:end] for start, end in inline_exact_zones(text)]
    assert zones == ["|:---|---:|"]
