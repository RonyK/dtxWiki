from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum


class AnchorStatus(StrEnum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    FUZZY_DISTANT = "fuzzy_distant"
    ORPHAN = "orphan"


@dataclass(frozen=True)
class ResolvedAnchor:
    char_offset: int | None
    end_offset: int | None
    status: AnchorStatus
    resolved_text: str | None


WINDOW = 200
FUZZY_RATIO_MIN = 0.80
DISTANT_RATIO_MIN = 0.60
CONTEXT_RATIO_MIN = 2 / 3
MAX_ANCHOR_LEN_FOR_FUZZY = 400
EARLY_EXIT_RATIO = 0.97
SMALL_ANCHOR_THRESHOLD = 30
# Distant fuzzy (window=None) scans the entire body — bound the body length
# to keep the resolver's CPU envelope predictable. body_md itself is capped
# at schemas.page.BODY_MD_MAX_LENGTH (1 MB) at the API edge, but a per-call
# cap here adds defense-in-depth and lets us treat oversized inputs as
# orphan without writing huge ResolvedAnchor results into the LRU cache.
MAX_BODY_LEN_FOR_DISTANT_FUZZY = 500_000


def resolve_anchor(
    body_md: str,
    *,
    anchor_text: str,
    context_before: str,
    context_after: str,
    char_offset_hint: int,
) -> ResolvedAnchor:
    if not body_md or not anchor_text:
        return orphan_anchor()

    exact = exact_match_near_hint(body_md, anchor_text, char_offset_hint)
    if exact is not None:
        return exact

    fuzzy = fuzzy_match(
        body_md,
        anchor_text=anchor_text,
        context_before=context_before,
        context_after=context_after,
        char_offset_hint=char_offset_hint,
        window=WINDOW,
        minimum_ratio=FUZZY_RATIO_MIN,
        status=AnchorStatus.FUZZY,
    )
    if fuzzy is not None:
        return fuzzy

    distant = fuzzy_match(
        body_md,
        anchor_text=anchor_text,
        context_before=context_before,
        context_after=context_after,
        char_offset_hint=char_offset_hint,
        window=None,
        minimum_ratio=DISTANT_RATIO_MIN,
        status=AnchorStatus.FUZZY_DISTANT,
    )
    return distant if distant is not None else orphan_anchor()


def exact_match_near_hint(
    body_md: str,
    anchor_text: str,
    char_offset_hint: int,
) -> ResolvedAnchor | None:
    anchor_length = len(anchor_text)
    if anchor_length == 0:
        return None
    if not 0 <= char_offset_hint <= len(body_md) - anchor_length:
        return None
    if body_md[char_offset_hint : char_offset_hint + anchor_length] != anchor_text:
        return None
    return ResolvedAnchor(
        char_offset=char_offset_hint,
        end_offset=char_offset_hint + anchor_length,
        status=AnchorStatus.EXACT,
        resolved_text=anchor_text,
    )


def fuzzy_match(
    body_md: str,
    *,
    anchor_text: str,
    context_before: str,
    context_after: str,
    char_offset_hint: int,
    window: int | None,
    minimum_ratio: float,
    status: AnchorStatus,
) -> ResolvedAnchor | None:
    anchor_length = len(anchor_text)
    if anchor_length == 0 or anchor_length > MAX_ANCHOR_LEN_FOR_FUZZY:
        return None

    if window is None:
        # Distant scan over the entire body — skip when the body is large
        # enough that the sweep would be expensive. Caller falls through to
        # orphan, which is the correct outcome for moved-and-rewritten anchors.
        if len(body_md) > MAX_BODY_LEN_FOR_DISTANT_FUZZY:
            return None
        region_start = 0
        region_end = len(body_md)
    else:
        region_start = max(0, char_offset_hint - window)
        region_end = min(len(body_md), char_offset_hint + window + len(anchor_text) * 2)
    region = body_md[region_start:region_end]
    if not region:
        return None

    step = 1 if anchor_length <= SMALL_ANCHOR_THRESHOLD else max(1, anchor_length // 20)
    min_length = max(1, anchor_length - anchor_length // 5)
    max_length = anchor_length + anchor_length // 5
    max_start = max(0, len(region) - min_length)
    best: tuple[float, float, int, str] | None = None
    normalized_anchor = normalize_text(anchor_text)
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(normalized_anchor)

    for start in range(0, max_start + 1, step):
        found_near_perfect = False
        for length in range(min_length, max_length + 1, step):
            end = start + length
            if end > len(region):
                break
            candidate = region[start:end]
            if has_mismatched_edge_whitespace(anchor_text, candidate):
                continue
            matcher.set_seq1(normalize_text(candidate))
            similarity = matcher.ratio()
            if similarity < minimum_ratio:
                continue
            context_similarity = context_ratio(
                context_before=context_before,
                context_after=context_after,
                region=region,
                start=start,
                end=end,
            )
            if context_similarity < CONTEXT_RATIO_MIN:
                continue
            absolute_start = region_start + start
            hint_distance = abs(absolute_start - char_offset_hint)
            score = (
                similarity,
                context_similarity,
                -hint_distance,
            )
            best_score = (
                best[0],
                best[1],
                -abs(best[2] - char_offset_hint),
            ) if best is not None else None
            if best_score is None or score > best_score:
                best = (similarity, context_similarity, absolute_start, candidate)
                # Sweep early-exit: only short-circuit when the best candidate
                # sits exactly at the stored char_offset_hint. Proposal C5 step
                # 2 treats the persisted hint as authoritative when the body is
                # unchanged; a higher-similarity candidate elsewhere in the
                # window can still be wrong if duplicate text exists. Requiring
                # hint_distance == 0 trades a small amount of sweep work for
                # duplicate-text disambiguation.
                if (
                    similarity >= EARLY_EXIT_RATIO
                    and context_similarity >= EARLY_EXIT_RATIO
                    and hint_distance == 0
                ):
                    found_near_perfect = True
                    break
        if found_near_perfect:
            break

    if best is None:
        return None
    _, _, absolute, candidate = best
    return ResolvedAnchor(
        char_offset=absolute,
        end_offset=absolute + len(candidate),
        status=status,
        resolved_text=candidate,
    )


def context_ratio(
    *,
    context_before: str,
    context_after: str,
    region: str,
    start: int,
    end: int,
) -> float:
    if context_before:
        before_start = max(0, start - len(context_before))
        score = text_ratio(context_before, region[before_start:start])
        if score < CONTEXT_RATIO_MIN:
            return score
    if context_after:
        after_end = min(len(region), end + len(context_after))
        score = text_ratio(context_after, region[end:after_end])
        if score < CONTEXT_RATIO_MIN:
            return score
    return 1.0


def text_ratio(left: str, right: str) -> float:
    return SequenceMatcher(
        None,
        normalize_text(left),
        normalize_text(right),
        autojunk=False,
    ).ratio()


def has_mismatched_edge_whitespace(anchor_text: str, candidate: str) -> bool:
    if anchor_text[0].strip() and candidate[0].isspace():
        return True
    if anchor_text[-1].strip() and candidate[-1].isspace():
        return True
    return False


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def orphan_anchor() -> ResolvedAnchor:
    return ResolvedAnchor(
        char_offset=None,
        end_offset=None,
        status=AnchorStatus.ORPHAN,
        resolved_text=None,
    )
