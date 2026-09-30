import time

from backend.services.anchor_resolver import (
    MAX_BODY_LEN_FOR_DISTANT_FUZZY,
    AnchorStatus,
    resolve_anchor,
)


def test_resolve_anchor_exact_match_near_hint() -> None:
    body = "Intro\n\nNeeds a source.\n\nOutro"

    resolved = resolve_anchor(
        body,
        anchor_text="Needs a source.",
        context_before="Intro\n\n",
        context_after="\n\nOutro",
        char_offset_hint=7,
    )

    assert resolved.status == AnchorStatus.EXACT
    assert resolved.char_offset == 7
    assert resolved.end_offset == 22
    assert resolved.resolved_text == "Needs a source."


def test_exact_match_prefers_hint_when_duplicate_text_present() -> None:
    body = "Claim\n\nMiddle\n\nClaim"
    second_offset = body.rindex("Claim")

    resolved = resolve_anchor(
        body,
        anchor_text="Claim",
        context_before="\n\n",
        context_after="",
        char_offset_hint=second_offset,
    )

    assert resolved.status == AnchorStatus.EXACT
    assert resolved.char_offset == second_offset
    assert resolved.end_offset == second_offset + len("Claim")


def test_exact_match_returns_first_when_hint_points_to_first_duplicate() -> None:
    body = "Claim\n\nMiddle\n\nClaim"

    resolved = resolve_anchor(
        body,
        anchor_text="Claim",
        context_before="",
        context_after="\n\n",
        char_offset_hint=0,
    )

    assert resolved.status == AnchorStatus.EXACT
    assert resolved.char_offset == 0


def test_fuzzy_match_uses_hint_distance_when_duplicate_text_drifted() -> None:
    body = "Claim\n\nMiddle\n\nClaim"
    second_offset = body.rindex("Claim")

    resolved = resolve_anchor(
        body,
        anchor_text="Claim",
        context_before="",
        context_after="",
        char_offset_hint=second_offset + 1,
    )

    assert resolved.status == AnchorStatus.FUZZY
    assert resolved.char_offset == second_offset


def test_resolve_anchor_fuzzy_match_with_context_near_hint() -> None:
    body = "Intro\n\nNeeds one source.\n\nOutro"

    resolved = resolve_anchor(
        body,
        anchor_text="Needs a source.",
        context_before="Intro\n\n",
        context_after="\n\nOutro",
        char_offset_hint=7,
    )

    assert resolved.status == AnchorStatus.FUZZY
    assert resolved.resolved_text == "Needs one source."


def test_resolve_anchor_fuzzy_distant_when_text_moved_and_changed() -> None:
    body = f"Intro\n\n{'Moved text. ' * 30}\n\nLater\n\nNeeds one source.\n\nOutro"

    resolved = resolve_anchor(
        body,
        anchor_text="Needs a source.",
        context_before="Later\n\n",
        context_after="\n\nOutro",
        char_offset_hint=7,
    )

    assert resolved.status == AnchorStatus.FUZZY_DISTANT
    assert resolved.resolved_text == "Needs one source."


def test_resolve_anchor_orphans_when_body_exceeds_distant_fuzzy_cap() -> None:
    # Bodies larger than MAX_BODY_LEN_FOR_DISTANT_FUZZY skip the whole-document
    # sweep to bound resolver CPU. Drifted anchors on huge pages return orphan
    # rather than triggering an expensive sweep.
    padding_size = MAX_BODY_LEN_FOR_DISTANT_FUZZY + 1
    body = "x" * padding_size + "\n\nLater\n\nNeeds one source.\n\nOutro"

    resolved = resolve_anchor(
        body,
        anchor_text="Needs a source.",
        context_before="Later\n\n",
        context_after="\n\nOutro",
        char_offset_hint=7,
    )

    assert resolved.status == AnchorStatus.ORPHAN
    assert resolved.char_offset is None
    assert resolved.resolved_text is None


def test_resolve_anchor_orphans_when_context_mismatches() -> None:
    body = "Wrong lead\n\nNeeds one source.\n\nWrong tail"

    resolved = resolve_anchor(
        body,
        anchor_text="Needs a source.",
        context_before="Expected lead\n\n",
        context_after="\n\nExpected tail",
        char_offset_hint=0,
    )

    assert resolved.status == AnchorStatus.ORPHAN
    assert resolved.char_offset is None
    assert resolved.end_offset is None
    assert resolved.resolved_text is None


def test_anchor_orphans_when_context_after_mismatches_even_if_before_matches() -> None:
    body = "Intro paragraph\n\nThe quick brown fox leaps\n\ncompletely different ending"
    hint = body.index("The quick")

    resolved = resolve_anchor(
        body,
        anchor_text="The quick brown fox jumps",
        context_before="Intro paragraph\n\n",
        context_after="\n\nOriginal ending text",
        char_offset_hint=hint,
    )

    assert resolved.status == AnchorStatus.ORPHAN


def test_anchor_orphans_when_context_before_mismatches_even_if_after_matches() -> None:
    body = "completely different intro\n\nThe quick brown fox leaps\n\nOutro"
    hint = body.index("The quick")

    resolved = resolve_anchor(
        body,
        anchor_text="The quick brown fox jumps",
        context_before="Original intro text\n\n",
        context_after="\n\nOutro",
        char_offset_hint=hint,
    )

    assert resolved.status == AnchorStatus.ORPHAN


def test_resolve_anchor_orphans_on_complete_rewrite() -> None:
    resolved = resolve_anchor(
        "Completely different page body.",
        anchor_text="Needs a source.",
        context_before="Intro\n\n",
        context_after="\n\nOutro",
        char_offset_hint=7,
    )

    assert resolved.status == AnchorStatus.ORPHAN


def test_fuzzy_resolution_completes_under_budget_for_large_anchor() -> None:
    anchor_text = "x" * 1500
    body = "y" * 1000 + anchor_text + "y" * 1000

    started_at = time.perf_counter()
    resolved = resolve_anchor(
        body,
        anchor_text=anchor_text,
        context_before="",
        context_after="",
        char_offset_hint=999,
    )
    elapsed = time.perf_counter() - started_at

    assert elapsed < 1.0, f"fuzzy resolution took {elapsed:.3f}s"
    assert resolved.status == AnchorStatus.ORPHAN


def test_exact_resolution_for_large_anchor_completes_under_budget() -> None:
    """Large anchors under the fuzzy cap still use the hinted exact slice first."""
    anchor_text = "x" * 350
    context_before = "p" * 17
    body = f"{context_before}{anchor_text}\n\nsuffix"
    hint = len(context_before)

    started_at = time.perf_counter()
    resolved = resolve_anchor(
        body,
        anchor_text=anchor_text,
        context_before=context_before,
        context_after="\n\nsuffix",
        char_offset_hint=hint,
    )
    elapsed = time.perf_counter() - started_at

    assert elapsed < 1.0, f"exact resolution took {elapsed:.3f}s"
    assert resolved.status == AnchorStatus.EXACT
    assert resolved.char_offset == hint


def test_fuzzy_resolution_in_sweep_path_completes_under_budget() -> None:
    """A near match below the fuzzy cap exercises the bounded sweep path."""
    anchor_text = "x" * 340
    context_before = "p" * 17
    body = f"{context_before}{anchor_text[:-1]}y\n\nsuffix"
    hint = len(context_before)

    started_at = time.perf_counter()
    resolved = resolve_anchor(
        body,
        anchor_text=anchor_text,
        context_before=context_before,
        context_after="\n\nsuffix",
        char_offset_hint=hint,
    )
    elapsed = time.perf_counter() - started_at

    assert elapsed < 1.0, f"fuzzy sweep took {elapsed:.3f}s"
    assert resolved.status == AnchorStatus.FUZZY
    assert resolved.char_offset == hint
    assert resolved.resolved_text != anchor_text
