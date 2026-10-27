import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.job import Job
from backend.models.mcp_trace import MCPToolCall
from backend.models.review import ReviewItem
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.policy.rbac import grant_workspace_membership
from backend.services.activity_service import _decode_activity_cursor
from backend.services.library_service import LibraryService
from backend.tests.conftest import (
    authenticate_test_client,
    authenticate_test_client_in_workspace,
)


def make_client(page_service) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        workspace_slug="default",
    )
    return client


def _create_context_page_with_pending_review(client, workspace_id, *, title):
    """P9.1: page-first creation never opens a review on its own -- submit
    for review explicitly so a pending ReviewItem exists, matching what the
    old `write_draft` route always produced as part of creating."""

    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = client.post(pages_url, json={"title": title, "body_md": f"# {title}"})
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    submitted = client.post(
        f"{pages_url}/{data['id']}/submit-for-review",
        json={
            "idempotency_key": f"phase8-activity-submit-{data['id']}",
            "version_id": data["version_id"],
            "expected_editing_revision": data["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text
    return created


def test_activity_and_settings_endpoints_project_workspace_state(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    _create_context_page_with_pending_review(
        client, workspace_id, title="Activity Draft"
    )

    activity = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all",
    )
    assert activity.status_code == 200
    activity_data = activity.json()["data"]
    assert activity_data["unread_count"] >= 1
    assert any(item["lane"] == "pending" for item in activity_data["items"])
    assert any(item["lane"] == "history" for item in activity_data["items"])
    assert any(
        item["target_title"] == "Activity Draft" for item in activity_data["items"]
    )

    with page_service.session_factory.begin() as session:
        mcp_client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == UUID(workspace_id))
        )
        mcp_client_name = mcp_client.name
        session.add(
            MCPToolCall(
                client_id=mcp_client.id,
                workspace_id=UUID(workspace_id),
                mcp_session_id=uuid4(),
                request_id=uuid4(),
                tool_name="search",
                input_summary={"query": "activity"},
                result_status="succeeded",
                result_summary="Search completed.",
                warnings=[],
                provider="unknown",
                model_id="unknown",
                params_summary={},
                client_self_reported=False,
                latency_ms=12,
            )
        )

    trace_activity = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&sub_tab=traces",
    )
    assert trace_activity.status_code == 200
    trace_items = trace_activity.json()["data"]["items"]
    assert any(
        item["target"]["target_kind"] == "mcp_tool_call"
        and item["title"] == "MCP search succeeded"
        and item["target_title"] == mcp_client_name
        for item in trace_items
    )

    with page_service.session_factory.begin() as session:
        LibraryService().register_library_item(
            session=session,
            workspace_id=UUID(workspace_id),
            canonical_uri="https://example.com/activity-job-source",
            title="Activity Job Source",
            raw_text="activity job source body",
        )

    live_activity = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&sub_tab=live",
    )
    assert live_activity.status_code == 200
    live_items = live_activity.json()["data"]["items"]
    assert any(
        item["target"]["target_kind"] == "job" and "Extraction queued" in item["title"]
        for item in live_items
    )

    settings = client.get(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
    )
    assert settings.status_code == 200
    policies = settings.json()["data"]
    assert policies["workspace"]["policy"]["slug"] == "default"
    assert policies["library"]["policy"]["manual_approval_required"] is True
    assert policies["agent_injection"]["policy"]["bundle_ttl_default_seconds"] == 3600


def test_activity_hides_pending_reviews_outside_viewer_audience(page_service):
    owner_client = make_client(page_service)
    workspace_id = owner_client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    _create_context_page_with_pending_review(
        owner_client, workspace_id, title="Audience-private review"
    )

    with page_service.session_factory() as session:
        review = session.scalar(
            select(ReviewItem).where(
                ReviewItem.workspace_id == UUID(workspace_id),
                ReviewItem.status == "pending",
            )
        )
        assert review is not None
        review_id = str(review.id)

    owner_activity = owner_client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all"
    )
    assert owner_activity.status_code == 200
    assert any(
        item["review_item_id"] == review_id
        for item in owner_activity.json()["data"]["items"]
    )

    viewer_client = TestClient(owner_client.app)
    authenticate_test_client(viewer_client, email="activity-viewer@example.com")
    with page_service.session_factory.begin() as session:
        viewer = session.scalar(
            select(User).where(User.email == "activity-viewer@example.com")
        )
        assert viewer is not None
        grant_workspace_membership(
            session,
            user_id=viewer.id,
            workspace_id=UUID(workspace_id),
            role="viewer",
        )

    viewer_activity = viewer_client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all"
    )
    assert viewer_activity.status_code == 200
    assert all(
        item["review_item_id"] != review_id
        for item in viewer_activity.json()["data"]["items"]
    )


def test_activity_limit_budget_reserves_review_decisions_but_not_failed_jobs(
    page_service,
):
    # The pending lane is fed by two sources: review decisions (review_item_id
    # set) and failed jobs (review_item_id None, via lane_for_job). Only review
    # decisions get reserved priority; failed jobs are pending-lane but compete
    # with non-pending rows by recency. The same two failed jobs are queried
    # twice, first older and then newer than the draft's review decision.
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    _create_context_page_with_pending_review(
        client, workspace_id, title="Decision Draft"
    )

    # Two failed jobs OLDER than the draft's review + history rows.
    with page_service.session_factory.begin() as session:
        review = session.scalar(
            select(ReviewItem).where(ReviewItem.workspace_id == UUID(workspace_id))
        )
        assert review is not None
        base_time = review.created_at
        old = base_time - timedelta(hours=1)
        job_ids = []
        for index in range(2):
            job = Job(
                workspace_id=UUID(workspace_id),
                job_type="library_extraction",
                trigger_type="manual",
                status="failed",
                updated_at=old - timedelta(minutes=index),
            )
            session.add(job)
            session.flush()
            job_ids.append(job.id)

    activity = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&limit=2",
    )
    assert activity.status_code == 200
    items = activity.json()["data"]["items"]
    assert len(items) == 2
    # The review decision is reserved.
    assert any(item["review_item_id"] is not None for item in items)
    # The newer non-pending row survives; the older failed jobs are crowded out
    # (a recency-blind budget would have kept the two failed jobs instead).
    assert any(item["lane"] != "pending" for item in items)
    assert not any(item["target"]["target_kind"] == "job" for item in items)
    # API order contract: survivors are returned newest-first (the recency
    # budget orders the final page), not in source/insertion order.
    created_ats = [item["created_at"] for item in items]
    assert created_ats == sorted(created_ats, reverse=True)

    # Now make both failed jobs strictly NEWER than the review decision. A
    # recency-only cap would keep both jobs at limit=2 and drop the older review
    # decision; the priority budget must keep the review decision instead.
    with page_service.session_factory.begin() as session:
        for index, job_id in enumerate(job_ids):
            job = session.get(Job, job_id)
            assert job is not None
            job.updated_at = base_time + timedelta(minutes=index + 1)

    activity = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&limit=2",
    )
    assert activity.status_code == 200
    items = activity.json()["data"]["items"]
    # One review decision + two failed jobs = three pending rows; only two return.
    assert len(items) == 2
    # Budget contract: the older review decision wins a slot over ONE of the two
    # newer failed jobs (a recency-only cap would keep both jobs and drop it).
    # Asserted as a set, since the response itself is recency-ordered.
    review_rows = [item for item in items if item["review_item_id"] is not None]
    job_rows = [
        item
        for item in items
        if item["lane"] == "pending" and item["target"]["target_kind"] == "job"
    ]
    assert len(review_rows) == 1
    assert review_rows[0]["target"]["target_kind"] == "change_set"
    assert len(job_rows) == 1


def test_activity_kind_classifies_jobs_and_lint_as_system(page_service):
    # v3: background jobs (target_kind=job) and lint audit rows are "System"
    # (platform/operations), not workspace content — they appear under kind=system
    # with auth/oauth and are absent from kind=workspace. Also a regression for the
    # budget/kind ordering: the split runs BEFORE the recency budget, so a single
    # older workspace content row survives a flood of NEWER system rows.
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    base = datetime.now(UTC) + timedelta(hours=1)
    with page_service.session_factory.begin() as session:
        # One workspace content audit row — the row kind=workspace must protect.
        session.add(
            AuditEvent(
                workspace_id=UUID(workspace_id),
                actor_kind="user",
                actor_id="tester",
                action="context_page.update",
                target_kind="context_page",
                target_id=uuid4(),
                created_at=base,
            )
        )
        # A failed job and a lint run — both now classified as System.
        session.add(
            Job(
                workspace_id=UUID(workspace_id),
                job_type="library_extraction",
                trigger_type="manual",
                status="failed",
                updated_at=base + timedelta(minutes=1),
            )
        )
        session.add(
            AuditEvent(
                workspace_id=UUID(workspace_id),
                actor_kind="user",
                actor_id="tester",
                action="lint.run.create",
                target_kind="lint_run",
                target_id=uuid4(),
                created_at=base + timedelta(minutes=2),
            )
        )
        # Five system sign-in events, each NEWER than the workspace row.
        for index in range(5):
            session.add(
                AuditEvent(
                    workspace_id=UUID(workspace_id),
                    actor_kind="user",
                    actor_id="tester",
                    action="auth.session.created",
                    target_kind="session",
                    target_id=uuid4(),
                    created_at=base + timedelta(minutes=index + 3),
                )
            )

    base = f"/api/v1/workspaces/{workspace_id}/activity?period=all"

    # kind=workspace drops ALL system rows (auth + job + lint) BEFORE the budget,
    # so the lone older workspace content row survives the flood (limit=3).
    workspace_items = client.get(f"{base}&limit=3&kind=workspace").json()["data"][
        "items"
    ]
    assert any(item["body"] == "context_page.update" for item in workspace_items)
    assert not any(
        item["target"]["target_kind"] == "job" for item in workspace_items
    )
    assert not any(
        item["body"].startswith(("auth.", "lint.")) for item in workspace_items
    )

    # kind=system now includes jobs and lint alongside auth/oauth.
    system_items = client.get(f"{base}&limit=20&kind=system").json()["data"]["items"]
    assert any(item["target"]["target_kind"] == "job" for item in system_items)
    assert any(item["body"].startswith("lint.") for item in system_items)
    assert any(item["body"].startswith("auth.") for item in system_items)
    assert not any(
        item["body"] == "context_page.update" for item in system_items
    )


def test_settings_change_appears_under_system_settings_activity(page_service):
    # A Settings-page change (workspace field / policy) is auditable System
    # activity (Settings sub-type): it emits a settings.* event that classifies
    # under kind=system and never under kind=workspace.
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    resp = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={"group": "workspace", "key": "title", "value": "Renamed Workspace"},
    )
    assert resp.status_code == 200, resp.text

    base = f"/api/v1/workspaces/{workspace_id}/activity?period=all&limit=20"
    system_items = client.get(f"{base}&kind=system").json()["data"]["items"]
    assert any(
        item["body"] == "settings.workspace.title.updated" for item in system_items
    )
    workspace_items = client.get(f"{base}&kind=workspace").json()["data"]["items"]
    assert not any(
        item["body"].startswith("settings.") for item in workspace_items
    )


def test_activity_kind_filter_applies_before_the_audit_limit(page_service):
    # Within the audit SOURCE itself: kind=workspace and kind=system are applied
    # in the audit QUERY (before its limit), not only after the merge. A
    # post-merge-only filter could never recover a row the source never fetched
    # because three newer rows of the other kind filled the audit page. Each
    # direction is staged right before its request, with its flood strictly newer
    # than everything already present.
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    def target_kind_of(action):
        return "session" if action.startswith("auth.") else "context_page"

    def stage_audit_rows(target_action, flood_action, base):
        with page_service.session_factory.begin() as session:
            session.add(
                AuditEvent(
                    workspace_id=UUID(workspace_id),
                    actor_kind="user",
                    actor_id="tester",
                    action=target_action,
                    target_kind=target_kind_of(target_action),
                    target_id=uuid4(),
                    created_at=base,
                )
            )
            # Three NEWER rows of the other kind that would fill a small page.
            for index in range(3):
                session.add(
                    AuditEvent(
                        workspace_id=UUID(workspace_id),
                        actor_kind="user",
                        actor_id="tester",
                        action=flood_action,
                        target_kind=target_kind_of(flood_action),
                        target_id=uuid4(),
                        created_at=base + timedelta(minutes=index + 1),
                    )
                )

    # A workspace audit row under three newer system sign-in audit rows.
    stage_audit_rows(
        "context_page.updated",
        "auth.session.created",
        datetime.now(UTC) + timedelta(hours=2),
    )
    items = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&kind=workspace&limit=2"
    ).json()["data"]["items"]
    # The workspace audit row is surfaced despite three newer system rows, and no
    # auth row leaks into the workspace view.
    assert any(item["body"] == "context_page.updated" for item in items)
    assert not any(item["body"].startswith("auth.") for item in items)

    # Mirror: an older auth row under three newer WORKSPACE audit rows. (Without
    # the kind=="system" SQL filter the newest rows fetched would be the
    # workspace ones, then dropped post-merge, so no auth row would surface.)
    stage_audit_rows(
        "auth.session.created",
        "context_page.updated",
        datetime.now(UTC) + timedelta(hours=4),
    )
    items = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&kind=system&limit=2"
    ).json()["data"]["items"]
    # Only system (auth) rows are returned despite three newer workspace rows; no
    # workspace audit row leaks into the system view.
    assert items
    assert all(item["body"].startswith("auth.") for item in items)
    assert not any(item["body"] == "context_page.updated" for item in items)


def test_activity_excludes_library_observation_events_before_the_page_limit(
    page_service,
):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    base = datetime.now(UTC) + timedelta(hours=3)
    with page_service.session_factory.begin() as session:
        session.add(
            AuditEvent(
                workspace_id=UUID(workspace_id),
                actor_kind="user",
                actor_id="tester",
                action="library.item.version.create",
                target_kind="library_item_version",
                target_id=uuid4(),
                created_at=base,
            )
        )
        for index, action in enumerate(
            (
                "library.artifact.read",
                "library.original.downloaded",
                "library.preview.source.read",
                "library.semantic_overlay.preview.read",
            ),
            start=1,
        ):
            session.add(
                AuditEvent(
                    workspace_id=UUID(workspace_id),
                    actor_kind="user",
                    actor_id="tester",
                    action=action,
                    target_kind="library_transport",
                    target_id=uuid4(),
                    created_at=base + timedelta(minutes=index),
                )
            )

    activity = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity",
        params={"period": "all", "lane": "history", "limit": 1},
    )
    assert activity.status_code == 200, activity.text
    items = activity.json()["data"]["items"]
    assert [item["body"] for item in items] == ["library.item.version.create"]


def test_activity_lane_pagination_cursor_tolerance_and_kind_validation(page_service):
    # The dedicated lane page paginates by recency via cursor/next_cursor; the
    # overview (no lane) keeps next_cursor None.
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    base = datetime.now(UTC) + timedelta(hours=3)
    with page_service.session_factory.begin() as session:
        for index in range(5):
            session.add(
                AuditEvent(
                    workspace_id=UUID(workspace_id),
                    actor_kind="user",
                    actor_id="tester",
                    action="context_page.updated",
                    target_kind="context_page",
                    target_id=uuid4(),
                    created_at=base + timedelta(minutes=index),
                )
            )

    base_url = (
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&lane=history&limit=2"
    )
    page1 = client.get(base_url).json()["data"]
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None

    page2 = client.get(f"{base_url}&cursor={page1['next_cursor']}").json()["data"]
    assert len(page2["items"]) == 2
    # Pages do not overlap and stay in recency order across the boundary.
    ids1 = {item["id"] for item in page1["items"]}
    ids2 = {item["id"] for item in page2["items"]}
    assert ids1.isdisjoint(ids2)
    assert min(item["created_at"] for item in page1["items"]) >= max(
        item["created_at"] for item in page2["items"]
    )

    # The overview (no lane) never paginates.
    overview = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&limit=2"
    ).json()["data"]
    assert overview["next_cursor"] is None

    # A malformed cursor is tolerated as the first page; a huge one is clamped.
    bad_cursor_page = client.get(f"{base_url}&cursor=not-a-valid-cursor!").json()[
        "data"
    ]
    assert len(bad_cursor_page["items"]) == 2
    assert bad_cursor_page["next_cursor"] is not None

    huge_cursor = base64.urlsafe_b64encode(
        b"activity-offset:100000000"
    ).decode()
    assert _decode_activity_cursor(huge_cursor) == 10_000
    huge_cursor_page = client.get(f"{base_url}&cursor={huge_cursor}").json()[
        "data"
    ]
    assert huge_cursor_page["items"] == []
    assert huge_cursor_page["next_cursor"] is None

    # kind is a constrained Literal; a typo is a 422, never a silent fall-through
    # to unfiltered "all" (which would leak system rows into a workspace view).
    resp = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&kind=workspce"
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("personal_offset", [0, -1], ids=["tie", "reversed"])
def test_settings_policy_patch_updates_workspace_and_literal_values(
    page_service, personal_offset
):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    # GET /workspaces orders by created_at, then slug. The seeded workspace
    # selected above and the Personal one signup created are stamped a fraction
    # of a second apart, so their order is whatever the wall clock did between
    # the two inserts (a backward step lists Personal first). Pin a tie and a
    # reversal so the rename check below cannot lean on list position.
    with page_service.session_factory.begin() as session:
        target = session.get(Workspace, UUID(workspace_id))
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        assert target is not None
        assert user is not None
        personal = session.scalars(
            select(Workspace)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(WorkspaceMembership.user_id == user.id, Workspace.id != target.id)
        ).one()
        personal.created_at = target.created_at + timedelta(seconds=personal_offset)

    language = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={"group": "workspace", "key": "default_language", "value": "ko"},
    )
    assert language.status_code == 200
    assert language.json()["data"]["workspace"]["policy"]["default_language"] == "ko"

    slug = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={"group": "workspace", "key": "slug", "value": "Research Space V2"},
    )
    assert slug.status_code == 200
    assert slug.json()["data"]["workspace"]["policy"]["slug"] == "research-space-v2"
    listed = client.get("/api/v1/workspaces")
    assert listed.status_code == 200
    # By id, never by position: both pinned orders list Personal first. The
    # rename must also show on exactly one listed workspace, so it cannot have
    # reached the user's other one as well.
    workspaces = listed.json()["data"]["workspaces"]
    matching_slugs = [item["slug"] for item in workspaces if item["id"] == workspace_id]
    assert matching_slugs == ["research-space-v2"]
    assert sum(item["slug"] == "research-space-v2" for item in workspaces) == 1

    strictness = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={"group": "citation", "key": "strictness", "value": "strict"},
    )
    assert strictness.status_code == 200
    assert strictness.json()["data"]["citation"]["policy"]["strictness"] == "strict"

    agent_literal = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "agent_injection",
            "key": "category_inclusion_policy.memory",
            "value": "never",
        },
    )
    assert agent_literal.status_code == 200
    assert (
        agent_literal.json()["data"]["agent_injection"]["policy"][
            "category_inclusion_policy"
        ]["memory"]
        == "never"
    )
    # The response above is built from the session that just mutated the row;
    # only a fresh read proves the nested change was actually stored.
    persisted = client.get(f"/api/v1/workspaces/{workspace_id}/settings/policies")
    assert persisted.status_code == 200
    assert (
        persisted.json()["data"]["agent_injection"]["policy"][
            "category_inclusion_policy"
        ]["memory"]
        == "never"
    )

    allowed_sources = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "library",
            "key": "allowed_sources",
            "value": [
                {"type": "web", "pattern": "https://docs.example.com/*"},
                {"type": "manual_upload"},
            ],
        },
    )
    assert allowed_sources.status_code == 422
    assert allowed_sources.json()["error_code"] == "library_source_rules_retired"

    denied_sources = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "library",
            "key": "denied_sources",
            "value": [{"type": "web", "pattern": "https://blocked.example/*"}],
        },
    )
    assert denied_sources.status_code == 422
    assert denied_sources.json()["error_code"] == "library_source_rules_retired"

    citation_types = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "citation",
            "key": "accepted_citation_types",
            "value": ["raw_source", "context_page"],
        },
    )
    assert citation_types.status_code == 200
    assert citation_types.json()["data"]["citation"]["policy"][
        "accepted_citation_types"
    ] == ["raw_source", "context_page"]

    review_policy = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "review",
            "key": "automation.auto_approve_after_hours",
            "value": 72,
        },
    )
    assert review_policy.status_code == 200
    assert review_policy.json()["data"]["review"]["policy"]["automation"][
        "auto_approve_after_hours"
    ] == 72

    review_preset = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={"group": "review", "key": "preset", "value": " fast_moving "},
    )
    assert review_preset.status_code == 200
    assert review_preset.json()["data"]["review"]["policy"]["preset"] == (
        "fast_moving"
    )

    rejected = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={"group": "workspace", "key": "default_language", "value": "xx"},
    )
    assert rejected.status_code == 422

    rejected_source = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "library",
            "key": "allowed_sources",
            "value": [{"type": "connector"}],
        },
    )
    assert rejected_source.status_code == 422
    assert rejected_source.json()["error_code"] == "library_source_rules_retired"

    rejected_blank_source = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "library",
            "key": "allowed_sources",
            "value": [{"type": "web", "pattern": " "}],
        },
    )
    assert rejected_blank_source.status_code == 422
    assert rejected_blank_source.json()["error_code"] == "library_source_rules_retired"

    rejected_citation = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "citation",
            "key": "accepted_citation_types",
            "value": ["unknown"],
        },
    )
    assert rejected_citation.status_code == 422

    rejected_review = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "review",
            "key": "automation.auto_approve_below",
            "value": "high",
        },
    )
    assert rejected_review.status_code == 422


def test_settings_policy_rejects_bundle_ttl_above_one_year(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    accepted = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "agent_injection",
            "key": "bundle_ttl_max_seconds",
            "value": 8760 * 3600,
        },
    )
    assert accepted.status_code == 200
    assert (
        accepted.json()["data"]["agent_injection"]["policy"][
            "bundle_ttl_max_seconds"
        ]
        == 8760 * 3600
    )

    rejected = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "agent_injection",
            "key": "bundle_ttl_default_seconds",
            "value": (8760 * 3600) + 1,
        },
    )
    assert rejected.status_code == 422
    assert "8760 hours" in rejected.json()["message"]


def test_settings_policy_batch_is_atomic_and_revision_guarded(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    policies = client.get(
        f"/api/v1/workspaces/{workspace_id}/settings/policies"
    ).json()["data"]
    initial = policies["library"]

    updated = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={
            "group": "library",
            "expected_revision": initial["revision"],
            "changes": {
                "max_upload_bytes": 7_000_000,
                "format_overrides": {"md": 3_000_000},
            },
        },
    )
    assert updated.status_code == 200, updated.text
    updated_card = updated.json()["data"]["library"]
    assert updated_card["policy"]["max_upload_bytes"] == 7_000_000
    assert updated_card["policy"]["format_overrides"]["md"] == 3_000_000
    assert updated_card["revision"] != initial["revision"]

    stale = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={
            "group": "library",
            "expected_revision": initial["revision"],
            "changes": {"max_upload_bytes": 8_000_000},
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "settings_revision_conflict"

    legacy_library_write = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={
            "group": "library",
            "key": "max_upload_bytes",
            "value": 8_000_000,
        },
    )
    assert legacy_library_write.status_code == 422
    assert (
        legacy_library_write.json()["error_code"]
        == "library_policy_batch_update_required"
    )

    retired = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={
            "group": "library",
            "expected_revision": updated_card["revision"],
            "changes": {"allowed_sources": []},
        },
    )
    assert retired.status_code == 422
    assert retired.json()["error_code"] == "library_source_rules_retired"


def test_settings_family_limit_may_exceed_workspace_default(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    initial = client.get(
        f"/api/v1/workspaces/{workspace_id}/settings/policies"
    ).json()["data"]["library"]

    updated = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={
            "group": "library",
            "expected_revision": initial["revision"],
            "changes": {
                "max_upload_bytes": 7_000_000,
                "format_families": [
                    {
                        "family_id": "office",
                        "name": "Office",
                        "extensions": [".docx", ".xlsx"],
                        "max_upload_bytes": 8_000_000,
                    }
                ],
            },
        },
    )

    assert updated.status_code == 200, updated.text
    library = updated.json()["data"]["library"]
    assert library["policy"]["max_upload_bytes"] == 7_000_000
    assert library["policy"]["format_families"] == [
        {
            "family_id": "office",
            "name": "Office",
            "origin": "custom",
            "format_ids": ["docx", "xlsx"],
            "max_upload_bytes": 8_000_000,
        }
    ]
    assert library["revision"] != initial["revision"]


def test_settings_mcp_client_status_update(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    with page_service.session_factory.begin() as session:
        external = MCPClient(
            workspace_id=UUID(workspace_id),
            name="external-cli",
            client_type="generic",
            scopes=["mcp:read"],
            status="active",
        )
        session.add(external)
        session.flush()
        external_id = str(external.id)

    disabled = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{external_id}",
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200
    clients = disabled.json()["data"]["mcp_clients"]
    assert next(item for item in clients if item["id"] == external_id)["status"] == (
        "disabled"
    )

    enabled = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{external_id}",
        json={"status": "active"},
    )
    assert enabled.status_code == 200
    clients = enabled.json()["data"]["mcp_clients"]
    assert next(item for item in clients if item["id"] == external_id)["status"] == (
        "active"
    )

def test_ask_session_turn_uses_context_first_search(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    needle_created = client.post(
        pages_url,
        json={"title": "Needle Context", "body_md": ""},
    ).json()["data"]
    # P9.1: page-first creation never publishes -- search only indexes
    # published/non-"editing" content, so publish it directly with a
    # follow-up PATCH (mirrors the retired `write_page` single-shot create).
    published_needle = client.patch(
        f"{pages_url}/{needle_created['id']}",
        json={
            "title": "Needle Context",
            "body_md": "# Needle Context\n\nNeedle evidence body.",
            "base_version_id": needle_created["version_id"],
            "base_content_hash": needle_created["content_hash"],
        },
    )
    assert published_needle.status_code == 200, published_needle.text

    created_session = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={"title": "Research Ask", "default_topic_tags": ["research"]},
    )
    assert created_session.status_code == 201
    ask_session = created_session.json()["data"]
    started_at = datetime.fromisoformat(
        ask_session["started_at"].replace("Z", "+00:00")
    )
    ask_tree = client.get(
        f"/api/v1/workspaces/{workspace_id}/tree?section=ask",
    ).json()["data"]["nodes"]
    assert [node["title"] for node in ask_tree] == [
        "Ask",
        f"{started_at:%Y}",
        f"{started_at:%m}",
        f"{started_at:%d}",
        "Research Ask",
    ]
    root = next(node for node in ask_tree if node["title"] == "Ask")
    year = next(
        node
        for node in ask_tree
        if node["title"] == f"{started_at:%Y}" and node["parent_id"] == root["id"]
    )
    month = next(
        node
        for node in ask_tree
        if node["title"] == f"{started_at:%m}" and node["parent_id"] == year["id"]
    )
    day = next(
        node
        for node in ask_tree
        if node["title"] == f"{started_at:%d}" and node["parent_id"] == month["id"]
    )
    session_node = next(
        node
        for node in ask_tree
        if node["title"] == "Research Ask" and node["parent_id"] == day["id"]
    )
    assert session_node["target_id"] == ask_session["id"]

    turn = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session['id']}/turns",
        json={"user_message": "Needle"},
    )
    assert turn.status_code == 200
    turn_data = turn.json()["data"]
    assert "Needle Context" in turn_data["assistant_message"]
    assert turn_data["cited_context_page_ids"]
    assert turn_data["fallback_disclosure"] is False
    assert turn.json()["envelope"]["warnings"] == []
    assert turn_data["retrieval_trace"]
    assert set(turn_data["retrieval_trace"][0]) == {
        "id",
        "type",
        "title",
        "score",
        "match_reason",
        "concept_path",
    }

    reread = client.get(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session['id']}"
    )
    assert reread.status_code == 200
    persisted_turn = reread.json()["data"]["turns"][0]
    assert persisted_turn["retrieval_trace"] == turn_data["retrieval_trace"]
    assert persisted_turn["tried_lanes"] == turn_data["tried_lanes"]
    assert persisted_turn["fallback_disclosure"] == turn_data["fallback_disclosure"]

    listed = client.get(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["items"][0]["turn_count"] == 1


def test_ask_zero_hit_records_coverage_gap_and_warning(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    created_session = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={"title": "Coverage Ask"},
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/"
        f"{created_session.json()['data']['id']}/turns",
        json={"user_message": "zzzz ask coverage gap"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["cited_context_page_ids"] == []
    assert response.json()["data"]["cited_library_artifact_ids"] == []
    assert [item["code"] for item in response.json()["envelope"]["warnings"]] == [
        "coverage_gap_recorded"
    ]
    with page_service.session_factory() as session:
        gap = session.scalar(
            select(KnowledgeCoverageGap).where(
                KnowledgeCoverageGap.workspace_id == UUID(workspace_id),
                KnowledgeCoverageGap.surface == "ask",
            )
        )
    assert gap is not None
    assert gap.query_norm == "zzzz ask coverage gap"


def test_ask_turn_rejects_invalid_user_message_with_422(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    created_session = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={"title": "Validation Ask"},
    )
    assert created_session.status_code == 201
    ask_session = created_session.json()["data"]

    rejected = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session['id']}/turns",
        json={"user_message": "   "},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error_code"] == "invalid_user_message"
