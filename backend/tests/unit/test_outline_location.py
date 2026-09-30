"""Offsets used against the raw outline must be raw offsets."""

from backend.services.retrieval.outline_location import heading_location_for_query


def test_query_hit_is_credited_to_the_right_heading_despite_case_folding():
    # `casefold()` expands "ß" to "ss", so a folded-copy offset runs ahead of
    # the raw one. With enough of that drift before a heading boundary, the hit
    # gets credited to the following section instead of the one it is in.
    drift = "ß" * 40
    body = (
        "# Doc\n\n## Erste\n\n"
        f"{drift} target-token hier.\n\n"
        "## Zweite\n\nsomething else\n"
    )
    section = heading_location_for_query(body, "target-token")

    assert section is not None
    assert section.heading_path == ("Doc", "Erste"), (
        "the hit is inside 'Erste'; a folded offset would push it into 'Zweite'"
    )


def test_query_matching_stays_case_insensitive():
    body = "# Doc\n\n## Alpha\n\nMixedCase Marker here\n"
    section = heading_location_for_query(body, "mixedcase")
    assert section is not None
    assert section.heading_path == ("Doc", "Alpha")


def test_query_hit_is_credited_to_the_section_with_the_distinctive_token():
    # "the" is a common word that appears early, in "Alpha". The query's
    # distinctive token, "widget-configurator", only appears later, in "Beta".
    # The result must be attributed to "Beta" -- the section the query was
    # actually about -- not to "Alpha" just because "the" happens to occur
    # earlier in the raw body.
    body = (
        "# Doc\n\n## Alpha\n\nHere is the introduction, nothing special.\n\n"
        "## Beta\n\nHere is the widget-configurator described in detail.\n"
    )
    section = heading_location_for_query(body, "the widget-configurator")

    assert section is not None
    assert section.heading_path == ("Doc", "Beta")
