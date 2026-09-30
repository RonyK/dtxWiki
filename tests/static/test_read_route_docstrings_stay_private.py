"""Three routes' internals must never reach the public schema again.

``test_public_schema_path_leak.py`` already gates the whole served document for
repo-path citations and internal shorthand, and deliberately (per its own
docstring) carries no per-route exemption list -- that gate is the right shape
for the general problem. This test is narrower and for a different failure
shape: none of these three routes' request internals (a design-item code, a
data-access model, a review-item number) trip that gate's path/shorthand
patterns at all, which is exactly how "P8.4 mask", "the sweep's own predicate"
and "One folder's routing index... fanout truncation" shipped to
/openapi.json unnoticed (260912 review).

All three had a `# Kept out of the docstring...` comment immediately above a
triple-quoted docstring FastAPI published anyway -- a comment is not a
statement, so the string after it stayed the docstring. get_settings_policies
in the same file already used the correct shape (a comment, nothing after it)
and is included below as the control case that must keep passing.

A per-route pin, not a blanket "routes may carry no docstring" rule: the
900-line file has dozens of one-line summaries that are legitimate API
documentation, and a blanket gate would be exactly the "treatment louder than
the disease" this repo's other schema gates explicitly avoid.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_OPENAPI_ARTIFACT = ROOT / "shared/schemas/rest/v1/openapi.json"

# (path, method, operationId prefix) for the three routes this fix demoted,
# plus one control case that was already correct and must stay that way.
_PINNED_ROUTES = (
    (
        "/api/v1/workspaces/{workspace_id}/tree/nodes/{node_id}/index-preview",
        "get",
        "get_tree_node_index_preview",
    ),
    (
        "/api/v1/workspaces/{workspace_id}/context/media/orphans",
        "get",
        "list_workspace_context_media_orphans",
    ),
    (
        "/api/v1/workspaces/{workspace_id}/context/media/attachments",
        "get",
        "list_workspace_context_media_attachments",
    ),
    (
        "/api/v1/workspaces/{workspace_id}/settings/policies",
        "get",
        "get_settings_policies",
    ),
)


def _operation(document: dict, path: str, method: str, operation_id_prefix: str):
    methods = document["paths"].get(path)
    assert methods is not None, f"{path} is not in the served schema at all"
    operation = methods.get(method)
    assert operation is not None, f"{method} {path} is not in the served schema"
    assert operation.get("operationId", "").startswith(operation_id_prefix), (
        f"{method} {path} operationId {operation.get('operationId')!r} does not "
        f"start with {operation_id_prefix!r} -- the route may have moved; update "
        "the path/prefix pin above rather than deleting the check"
    )
    return operation


def test_pinned_routes_publish_no_description():
    document = json.loads(_OPENAPI_ARTIFACT.read_text(encoding="utf-8"))
    leaking = []
    for path, method, prefix in _PINNED_ROUTES:
        operation = _operation(document, path, method, prefix)
        description = operation.get("description")
        if description:
            leaking.append(f"{method} {path}: {description[:80]!r}")

    assert leaking == [], (
        "A route that was fixed to keep its internals out of /openapi.json "
        "(or the control case that was already correct) now publishes a "
        "description again -- a docstring came back. Move the text to a `#` "
        "comment; see the module docstring for why this file's blanket path/"
        "shorthand gate does not catch this class of leak.\n  "
        + "\n  ".join(leaking)
    )


@pytest.mark.counterexample(
    "a restored docstring on a pinned route must be flagged as a leak"
)
def test_detector_catches_a_restored_docstring():
    """Counterexample: hand a route the exact shape that shipped the leak."""

    operation_id = (
        "get_tree_node_index_preview_api_v1_workspaces__workspace_id__"
        "tree_nodes__node_id__index_preview_get"
    )
    description = (
        "One folder's routing index, rendered exactly as get_index returns it."
    )
    document = {
        "paths": {
            "/api/v1/workspaces/{workspace_id}/tree/nodes/{node_id}/index-preview": {
                "get": {
                    "operationId": operation_id,
                    "description": description,
                }
            }
        }
    }

    leaking = []
    for path, method, prefix in _PINNED_ROUTES[:1]:
        operation = _operation(document, path, method, prefix)
        description = operation.get("description")
        if description:
            leaking.append(f"{method} {path}: {description[:80]!r}")

    assert leaking != [], "the detector failed to flag a restored docstring"
