import json
from pathlib import Path

from backend.utils.content_hash import canonicalize_markdown, content_hash

FIXTURE_PATH = Path("shared/fixtures/content_hash_vectors.json")


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_content_hash_matches_shared_vectors():
    fixture = load_fixture()

    for vector in fixture["vectors"]:
        expected_hex = vector["expected_hex"]
        assert content_hash(vector["input"]) == expected_hex, vector["id"]
        for equivalent_input in vector.get("equivalent_inputs", []):
            assert content_hash(equivalent_input) == expected_hex, vector["id"]


def test_canonical_text_is_available_for_contract_debugging():
    assert canonicalize_markdown("a   \r\nb\t\n\n") == "a\nb"


def test_frontmatter_version_no_line_is_not_excluded_from_the_hash():
    # Regression: content_hash used to strip a leading `---` block's
    # version_no/last_modified_at lines, so two bodies differing ONLY in
    # one of those hashed identically -- a full lost-update hole for any
    # CAS precondition built on this hash. A body opening with `---`
    # frontmatter is ordinary caller-supplied content (Jekyll/Hugo/Obsidian
    # imports routinely have one), not something the server assigns.
    older = "---\nversion_no: 2\n---\n\n# H\n\ntext\n"
    newer = "---\nversion_no: 3\n---\n\n# H\n\ntext\n"
    assert content_hash(older) != content_hash(newer)


def test_frontmatter_last_modified_at_line_is_not_excluded_from_the_hash():
    older = "---\nlast_modified_at: 2026-05-04T00:00:00Z\n---\nbody"
    newer = "---\nlast_modified_at: 2026-05-05T00:00:00Z\n---\nbody"
    assert content_hash(older) != content_hash(newer)
