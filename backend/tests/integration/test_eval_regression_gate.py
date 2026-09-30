import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.workspace import Workspace
from backend.services.eval.harness import (
    compare_baseline,
    evaluate_profile,
    load_golden,
)
from backend.services.page_service import sync_page_projections
from backend.services.search_service import build_search_service

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _page(session, workspace_id, slug, title, body=None):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type="concept",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=body or f"# {title}\n\nEvaluation body.",
        content_hash=uuid4().hex,
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="eval-gate",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.current_published_version_id = version.id
    sync_page_projections(session, page=page, version=version)
    return page


def _seed(session):
    workspace_id = session.scalar(
        select(Workspace.id).where(Workspace.slug == "default")
    )
    _page(session, workspace_id, "gate-exact-guide", "Gate Exact Guide")
    _page(
        session,
        workspace_id,
        "gate-deployment-checklist",
        "Gate Deployment Checklist",
    )
    _page(
        session,
        workspace_id,
        "gate-body-source",
        "Gate Body Source",
        "# Gate Body Source\n\nPhoton calibration evidence lives here.",
    )
    source = _page(
        session,
        workspace_id,
        "gate-associative-source",
        "Gate Associative Source",
    )
    related = _page(
        session,
        workspace_id,
        "gate-associative-related",
        "Operational Memory",
    )
    session.add(
        EdgeProjection(
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=source.id,
            target_kind="context_page",
            target_id=related.id,
            edge_kind="wikilink",
            provenance="curated",
            weight=1.0,
            source_ref={},
        )
    )
    return workspace_id


def _measure(service, session, workspace_id):
    golden = load_golden(
        json.loads((FIXTURES / "eval_golden.json").read_text(encoding="utf-8"))
    )
    return evaluate_profile(
        service,
        session,
        workspace_id,
        golden,
        modes=["keyword"],
        k=8,
    )


def test_eval_regression_gate_consumes_live_golden_and_baseline(session_factory):
    service = build_search_service(Settings(search_associative_lane=True))
    with session_factory.begin() as session:
        workspace_id = _seed(session)
    with session_factory() as session:
        modes, lanes = _measure(service, session, workspace_id)
    baseline = json.loads((FIXTURES / "eval_baseline.json").read_text(encoding="utf-8"))
    assert compare_baseline(modes, lanes, baseline) == []
    assert {item.lane for item in lanes} == {
        "entity",
        "trigram",
        "bm25",
        "associative",
    }


def test_eval_regression_gate_detects_a_live_search_drop(session_factory):
    service = build_search_service(Settings(search_associative_lane=True))
    with session_factory.begin() as session:
        workspace_id = _seed(session)

    class DroppingSearch:
        def search(self, *args, **kwargs):
            payload = service.search(*args, **kwargs)
            if kwargs.get("query") == "Gate Exact Guide":
                return payload.model_copy(update={"ranked_results": []})
            return payload

    with session_factory() as session:
        modes, lanes = _measure(DroppingSearch(), session, workspace_id)
    baseline = json.loads((FIXTURES / "eval_baseline.json").read_text(encoding="utf-8"))
    regressions = compare_baseline(modes, lanes, baseline)
    assert any(item.name == "entity" for item in regressions)
