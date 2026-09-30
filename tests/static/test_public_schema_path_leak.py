"""Keep repo-internal references out of the published API schemas.

``/docs``, ``/redoc`` and ``/openapi.json`` are mounted without an auth
override, so every description in that document is world-readable. Those
descriptions are not written where they are read: FastAPI and Pydantic lift
them out of route and model docstrings, so an internal design-doc citation
("doc/Epic-2/...md"), a source-file name, a dotted module path
("backend.models.job.Job") or a review-item number ("Codex #24") reads as an
ordinary code comment in the file being edited and only becomes public two
generators later. The same docstrings feed the MCP tool contracts, so one
model docstring leaked into both surfaces at once.

That is not hypothetical. backend/api/admin.py, backend/api/public.py,
backend/api/read.py, backend/schemas/jobs.py, backend/schemas/context_media.py,
backend/schemas/context_write.py and backend/api/oauth.py were all edited
during Phase 1 with those citations sitting in the docstrings, and the leak
survived each of those edits. Reading the Python file is demonstrably not
enough, so the check runs against the built schemas.

Nor is the shape of a leak fixed. An earlier version of this gate matched only
"<dir>/..." and "<file>.py", and a dotted module reference walked straight
through it into the served document; an earlier version matched only
Epic/Sprint/Phase/Task and D-N, and "Codex #24" sat on a public anonymous
route with every test green. Both holes are pinned by self-tests below, and
the detector is deliberately kept free of a per-description exemption list:
an exemption reads as an inventory of the remaining leaks, and it is only
ever an inventory of the leaks the pattern could see.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from backend.scripts.export_mcp_schemas import build_mcp_schemas
from backend.scripts.export_openapi import build_openapi_schema

ROOT = Path(__file__).resolve().parents[2]

_OPENAPI_ARTIFACT = ROOT / "shared/schemas/rest/v1/openapi.json"
_MCP_ARTIFACT_DIR = ROOT / "shared/schemas/mcp/v1"
_GENERATED_TYPES = ROOT / "frontend/src/shared/api/types.gen.ts"

# Top-level directories of this repository. Naming one of them followed by "/"
# is citing the source tree, never documenting the API: a consumer of the
# public schema has no such tree.
_REPO_DIRECTORIES = (
    "alembic",
    "backend",
    "desktop",
    "doc",
    "docs",
    "e2e",
    "frontend",
    "integrations",
    "internal",
    "packaging",
    "review",
    "scripts",
    "shared",
    "tests",
)

# Top-level import packages, for the dotted form of the same citation.
_REPO_PACKAGES = ("alembic", "backend", "desktop", "scripts", "tests")

_DIRECTORIES = "|".join(_REPO_DIRECTORIES)
_PACKAGES = "|".join(_REPO_PACKAGES)

# Four shapes, because a source citation has four shapes.
#
# The path alternatives are deliberately NOT just "<dir>/<anything>". This
# product is a Markdown wiki, so "Toggle a page between shared/private
# visibility", "Choose an internal/external audience" and "Upload notes.md"
# are ordinary consumer prose that a "<dir>/." pattern flags. An over-broad
# gate gets switched off rather than obeyed, so a bare "<dir>/<word>" with no
# further path structure and no extension is accepted, and a bare "<name>.md"
# is accepted too -- an internal doc citation in this repo is always
# path-qualified ("doc/Epic-2/...md"), which the first alternative catches.
_INTERNAL_PATH = re.compile(
    # A tree path, two or more segments deep: "doc/Epic-2/plan.md",
    # "backend/services/job_read_service.py".
    r"(?<![\w/.-])(?:" + _DIRECTORIES + r")/(?:[\w.-]+/)+[\w.-]+"
    # A file sitting directly in a top-level directory: "backend/models.py".
    r"|(?<![\w/.-])(?:" + _DIRECTORIES + r")/[\w-]+\.[A-Za-z0-9]{1,5}(?![\w])"
    # The directory itself: "nothing in backend/ deletes a version row".
    r"|(?<![\w/.-])(?:" + _DIRECTORIES + r")/(?![\w.-])"
    # The dotted form of a path, which cites the implementation exactly as
    # precisely: "backend.models.job.Job", "backend.services.job_read_service".
    r"|(?<![\w/.-])(?:" + _PACKAGES + r")\.[a-z_]\w*"
    # A bare source filename ("job_read_service.py"). ".md" is excluded here
    # on purpose -- see the note above about wiki prose.
    r"|(?<![\w/.-])[\w-]+\.(?:py|ts|tsx)(?![\w])"
)

# Internal shorthand names a private schedule, design item or review thread,
# so it dates the API and tells a consumer nothing.
_INTERNAL_SHORTHAND = re.compile(
    # Schedule shorthand: "Epic-2", "Phase 6a", "Sprint_5", "Task 3".
    # Capitalised only: "the second phase of the upload" is ordinary English.
    r"(?<![\w-])(?:Epic|Sprint|Phase|Task)[ _-]\d+[a-z]?(?![\w])"
    # Design/decision item codes: "D-7", "C-6", "AC-3". At most two capitals
    # and at most two digits, so external citations keep working:
    # "RFC-7009", "ISO-8601", "SHA-256", "UTF-8" and "P-256" all pass.
    r"|(?<![\w-])[A-Z]{1,2}-\d{1,2}(?![\w])"
    # Review-thread and tracker items: "Codex #24", "PR #113".
    r"|(?<![\w-])(?:Codex|Claude|Cursor|Copilot|GH|PR|MR|Issue|Ticket)\s*#\s*\d+"
    # A commit named as such: "commit 8dab0196". A bare hex run is not
    # matched -- this repo's public descriptions talk about hashed upload
    # filenames, which look identical.
    r"|(?<![\w-])[Cc]ommit\s+[0-9a-f]{7,40}(?![\w])"
    # A bare issue reference: "(#24)".
    r"|(?<![\w-])#\d+(?![\w])"
)


def _iter_strings(node: object, trail: list[str]):
    """Every string value in the document, with the key path that reached it.

    Deliberately not filtered to a "description"/"summary" key allow-list: an
    enum label, a title or an example is served just as publicly, and an
    allow-list of the places a leak was last seen is what let this one survive.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_strings(value, [*trail, str(key)])
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_strings(value, [*trail, str(index)])
    elif isinstance(node, str):
        yield "/".join(trail), node


def _document_leaks(
    document: object,
    pattern: re.Pattern[str] = _INTERNAL_PATH,
) -> list[str]:
    violations = []
    for trail, value in _iter_strings(document, []):
        match = pattern.search(value)
        if match:
            violations.append(f"{trail}: {match.group(0)!r}")
    return violations


def _text_leaks(source: str, pattern: re.Pattern[str] = _INTERNAL_PATH) -> list[str]:
    """Line-oriented scan of a generated text file.

    Every line is inspected, not only the ``*``-prefixed continuation lines of
    a block comment: openapi-typescript collapses a short description onto a
    single ``/** ... */`` line, so restricting the scan to the comment shape
    the leak was last seen in makes the gate blindest to the shortest leaks --
    the same allow-list mistake ``_iter_strings`` refuses to make.
    """
    violations = []
    for number, line in enumerate(source.splitlines(), start=1):
        match = pattern.search(line)
        if match:
            violations.append(f"line {number}: {match.group(0)!r}")
    return violations


@lru_cache(maxsize=1)
def _served_openapi() -> object:
    return build_openapi_schema()


@lru_cache(maxsize=1)
def _served_mcp() -> dict[str, object]:
    return build_mcp_schemas()


def test_served_openapi_document_has_no_repo_internal_paths():
    assert _document_leaks(_served_openapi()) == [], (
        "A public route or response-model docstring cites the source tree. "
        "Rewrite the description for an API consumer and move the internal "
        "rationale to a `#` comment, which never reaches the schema."
    )


def test_served_openapi_document_has_no_internal_planning_shorthand():
    leaks = _document_leaks(_served_openapi(), pattern=_INTERNAL_SHORTHAND)

    assert leaks == [], (
        "A public description names an internal epic/phase/design item or "
        "review thread. Describe the behaviour instead; the schedule and the "
        "item number belong in a `#` comment."
    )


def test_mcp_tool_schemas_have_no_repo_internal_paths():
    """Shared response models reach MCP clients too.

    PatchOperationResolution's docstring named three source files and shipped
    them in update_draft's contract as well as in the OpenAPI document, so
    gating only the REST side would have caught half of that one leak.
    """
    leaks = {
        tool_name: _document_leaks(schema)
        for tool_name, schema in _served_mcp().items()
        if _document_leaks(schema)
    }

    assert leaks == {}, (
        "An MCP tool contract cites the source tree; the description most "
        "likely comes from a shared backend.schemas model docstring."
    )


def test_mcp_tool_schemas_have_no_internal_planning_shorthand():
    leaks = {
        tool_name: _document_leaks(schema, pattern=_INTERNAL_SHORTHAND)
        for tool_name, schema in _served_mcp().items()
        if _document_leaks(schema, pattern=_INTERNAL_SHORTHAND)
    }

    assert leaks == {}, (
        "An MCP tool contract names an internal epic/phase/design item or "
        "review thread."
    )


def test_published_openapi_artifact_has_no_internal_references():
    """The built schema and the checked-in artifact are separate surfaces.

    `/openapi.json` serves the former; the client generator and anyone reading
    the repo consume the latter, and a hand-edit to it would pass the checks
    above.
    """
    artifact = json.loads(_OPENAPI_ARTIFACT.read_text(encoding="utf-8"))

    assert _document_leaks(artifact) == [], (
        "shared/schemas/rest/v1/openapi.json cites the source tree. "
        "Run `make export-schemas` after fixing the docstring."
    )
    assert _document_leaks(artifact, pattern=_INTERNAL_SHORTHAND) == [], (
        "shared/schemas/rest/v1/openapi.json names an internal epic/phase/"
        "design item. Run `make export-schemas` after fixing the docstring."
    )


def test_published_mcp_artifacts_have_no_internal_references():
    """Same argument, applied to the checked-in MCP tool contracts.

    These are what an MCP client integrator reads and what review diffs show;
    a hand-edit to one of them reaches a consumer without ever going through
    build_mcp_schemas(), exactly like a hand-edit to the REST artifact.
    """
    artifacts = sorted(_MCP_ARTIFACT_DIR.glob("*.json"))
    assert artifacts, f"no MCP tool contracts found under {_MCP_ARTIFACT_DIR}"

    leaks = {}
    for path in artifacts:
        contract = json.loads(path.read_text(encoding="utf-8"))
        found = _document_leaks(contract) + _document_leaks(
            contract, pattern=_INTERNAL_SHORTHAND
        )
        if found:
            leaks[path.name] = found

    assert leaks == {}, (
        "A checked-in MCP tool contract cites the source tree or names an "
        "internal epic/phase/design item. Run `make export-schemas` after "
        "fixing the docstring."
    )


def test_generated_typescript_comments_have_no_repo_internal_paths():
    source = _GENERATED_TYPES.read_text(encoding="utf-8")

    assert _text_leaks(source) == [], (
        "frontend/src/shared/api/types.gen.ts carries the leak forward in its "
        "doc comments. Run `make gen-client` after fixing the docstring."
    )


def test_generated_typescript_comments_have_no_internal_planning_shorthand():
    source = _GENERATED_TYPES.read_text(encoding="utf-8")

    assert _text_leaks(source, pattern=_INTERNAL_SHORTHAND) == [], (
        "frontend/src/shared/api/types.gen.ts carries an internal epic/phase/"
        "design item forward. Run `make gen-client` after fixing the docstring."
    )


def test_detector_reaches_a_description_nested_below_a_property():
    document = {
        "components": {
            "schemas": {
                "AdminJobListData": {
                    "properties": {
                        "unreachable_workspace_ids": {
                            "description": (
                                "Phase 6a fan-out "
                                "(doc/Epic-2/260828_workspace_db_isolation_plan.md "
                                "§4)."
                            )
                        }
                    }
                }
            }
        }
    }

    assert _document_leaks(document) == [
        "components/schemas/AdminJobListData/properties/"
        "unreachable_workspace_ids/description: "
        "'doc/Epic-2/260828_workspace_db_isolation_plan.md'"
    ]
    assert _document_leaks(document, pattern=_INTERNAL_SHORTHAND) == [
        "components/schemas/AdminJobListData/properties/"
        "unreachable_workspace_ids/description: 'Phase 6a'"
    ]


def test_detector_flags_a_dotted_module_reference():
    """The dotted form of a path is the same leak and used to pass.

    This is the exact string an adversarial probe put into JobRead's docstring
    while all of this file's tests stayed green; it reached
    ``components/schemas/JobRead/description`` in the served document.
    """
    document = {
        "components": {
            "schemas": {
                "JobRead": {
                    "description": (
                        "Mirrors backend.models.job.Job; projection in "
                        "backend.services.job_read_service.JobReadService "
                        "(control-plane session)."
                    )
                }
            }
        }
    }

    assert _document_leaks(document) == [
        "components/schemas/JobRead/description: 'backend.models'"
    ]


def test_detector_flags_commit_and_review_shorthand():
    """Review-thread and design-item codes are internal shorthand too.

    Both of these sat in the served document -- 'Codex #24' on a public
    anonymous-reader route -- while the shorthand pattern covered only
    Epic/Sprint/Phase/Task and D-N.
    """
    document = {
        "paths": {
            "/api/v1/public/w/{slug}/pages/{page}/uploads/{filename}": {
                "get": {
                    "description": (
                        "Serve an image referenced by a PUBLIC published "
                        "page (Codex #24)."
                    )
                }
            },
            "/api/v1/workspaces/{workspace_id}/tree/nodes/{node_id}/index-preview": {
                "get": {"description": "C-6 index preview: the get_index render."}
            },
            "/api/v1/admin/jobs": {
                "get": {"description": "Rewritten in commit 8dab0196 (#412)."}
            },
        }
    }

    assert sorted(_document_leaks(document, pattern=_INTERNAL_SHORTHAND)) == sorted(
        [
            "paths//api/v1/public/w/{slug}/pages/{page}/uploads/{filename}/get/"
            "description: 'Codex #24'",
            "paths//api/v1/workspaces/{workspace_id}/tree/nodes/{node_id}/"
            "index-preview/get/description: 'C-6'",
            "paths//api/v1/admin/jobs/get/description: 'commit 8dab0196'",
        ]
    )


def test_typescript_scanner_sees_a_single_line_doc_comment():
    """openapi-typescript collapses short descriptions onto one line.

    types.gen.ts carries ~2800 single-line ``/** ... */`` comments, ~1000 of
    them ``@description``. A scanner that only read ``*``-prefixed lines saw
    none of them, so the shorter the leaking description the safer it was.
    """
    source = "\n".join(
        [
            "export interface operations {",
            "    /** @description Successful Response (see backend/api/admin.py) */",
            "    admin_jobs_summary: never;",
            "}",
        ]
    )

    assert _text_leaks(source) == ["line 2: 'backend/api/admin.py'"]


def test_detector_accepts_consumer_facing_prose():
    """URL paths, media types, spec citations and wiki nouns are not leaks.

    A pattern that trips on these would be turned off rather than obeyed. The
    wiki cases are real product vocabulary: pages have shared/private
    visibility, audiences are internal/external, and the files a user uploads
    to a Markdown wiki are called things like notes.md.
    """
    document = {
        "paths": {
            "/api/v1/admin/jobs": {
                "get": {
                    "description": (
                        "Cross-workspace job listing. Not workspace-scoped -- "
                        "that is `GET /workspaces/{id}/jobs`."
                    )
                }
            },
            "/oauth/revoke": {
                "post": {
                    "description": (
                        "RFC 7009 OAuth 2.0 Token Revocation. Body is "
                        "application/x-www-form-urlencoded; returns 200 for an "
                        "unknown token (RFC 7009 §2.2). Bearer auth is checked "
                        "first; form client_id is phase 2 of that resolution."
                    )
                }
            },
            "/api/v1/workspaces/{workspace_id}/pages/{slug}": {
                "patch": {
                    "description": (
                        "Toggle a page between shared/private visibility and "
                        "choose an internal/external audience for it."
                    )
                }
            },
            "/api/v1/workspaces/{workspace_id}/uploads": {
                "post": {
                    "description": (
                        "Upload a Markdown file such as notes.md; the page is "
                        "returned as guide.md for download. Digests are "
                        "SHA-256 over UTF-8 bytes; keys may be RFC-7009 or "
                        "P-256 encoded and dates are ISO-8601."
                    )
                }
            },
        }
    }

    assert _document_leaks(document) == []
    assert _document_leaks(document, pattern=_INTERNAL_SHORTHAND) == []
