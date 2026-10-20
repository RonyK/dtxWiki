"""Operator metadata, cross-workspace cursor and guarded orphan cleanup."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.context_media.adapters.sqlalchemy import SqlAlchemyContextMediaRepository
from backend.models.audit import AuditEvent
from backend.models.context_media import ContextMediaAsset, ContextMediaUsage
from backend.models.workspace import Workspace
from backend.services.admin_context_media import list_admin_attachments
from backend.tests.integration.test_admin_jobs_fanout import (
    MultiShardProvider,
    _build_admin_client,
    _make_shard,
    _register_reachable_workspace,
)
from backend.tests.integration.test_context_media_attachments_api import (
    _seed_asset,
    _user_id,
    make_client,
)
from backend.tests.integration.test_context_media_orphan_api import (
    make_server_admin_client,
)

URL = "/api/v1/admin/context-media/attachments"


def _workspace(factory, title):
    workspace_id = uuid4()
    with factory.begin() as session:
        session.add(
            Workspace(id=workspace_id, slug=workspace_id.hex, title=title, name=title)
        )
    return workspace_id


def _seed(service, workspace_id, actor_id, **kwargs):
    return _seed_asset(service, workspace_id, created_by_actor_id=actor_id, **kwargs)


def _delete(client, row, **kwargs):
    return client.delete(
        f"{URL}/{row['asset_id']}",
        params={"workspace_id": row["workspace_id"]},
        headers={"If-Match": kwargs.get("token", row["usage_token"])},
    )


def test_operator_permissions_are_separate_from_workspace_owner(page_service):
    owner = make_client(page_service, "regular-owner@example.com")
    assert owner.get(URL).status_code == 403
    assert (
        owner.delete(
            f"{URL}/{uuid4()}",
            params={"workspace_id": uuid4()},
            headers={"If-Match": "x"},
        ).status_code
        == 403
    )
    owner.cookies.clear()
    assert owner.get(URL).status_code == 401


def test_admin_lists_nonmember_workspaces_without_preview_and_preserves_normal_acl(
    page_service,
):
    admin = make_server_admin_client(page_service, "operator@example.com")
    actor = _user_id(page_service, "operator@example.com")
    wid = _workspace(page_service.session_factory, "Other team")
    _seed(page_service, wid, actor, filename="orphan.png")
    _seed(page_service, wid, actor, filename="private-edit.png", editing=True)
    response = admin.get(URL)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    rows = response.json()["data"]["items"]
    assert [r["original_filename"] for r in rows] == ["orphan.png"]
    assert rows[0]["workspace_title"] == "Other team"
    assert rows[0]["can_delete"] is True
    assert rows[0]["content_url"] is None
    assert (
        admin.get(f"/api/v1/workspaces/{wid}/context/media/attachments").status_code
        == 404
    )


def test_admin_cursor_crosses_workspace_boundaries_searches_and_binds_filters(
    page_service,
):
    admin = make_server_admin_client(page_service, "paging-operator@example.com")
    actor = _user_id(page_service, "paging-operator@example.com")
    expected = set()
    for title, count in [("Team A", 3), ("Team B", 2)]:
        wid = _workspace(page_service.session_factory, title)
        for i in range(count):
            expected.add(
                _seed(page_service, wid, actor, filename=f"shot-{i}%.png")["asset_id"]
            )
    found = []
    cursor = None
    for _ in range(4):
        response = admin.get(
            URL, params={"limit": 2, **({"cursor": cursor} if cursor else {})}
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        found += [row["asset_id"] for row in data["items"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert set(found) == expected
    assert len(found) == len(expected)
    search = admin.get(URL, params={"q": "%", "limit": 2}).json()["data"]
    assert len(search["items"]) == 2
    assert (
        admin.get(
            URL, params={"q": "different", "cursor": search["next_cursor"]}
        ).status_code
        == 422
    )
    assert admin.get(URL, params={"cursor": "not-a-cursor"}).status_code == 422
    assert admin.get(URL, params={"q": "no-match"}).json()["data"]["items"] == []
    assert len(admin.get(URL, params={"mine": True}).json()["data"]["items"]) == 5


def test_admin_cleanup_checks_workspace_usage_token_and_audits(page_service):
    admin = make_server_admin_client(page_service, "cleanup-operator@example.com")
    actor = _user_id(page_service, "cleanup-operator@example.com")
    wid = _workspace(page_service.session_factory, "Cleanup")
    _seed(page_service, wid, actor)
    row = admin.get(URL, params={"orphan_only": True}).json()["data"]["items"][0]
    assert _delete(admin, {**row, "workspace_id": str(uuid4())}).status_code == 404
    assert _delete(admin, row, token="stale").status_code == 412
    assert _delete(admin, row).status_code == 200
    assert admin.get(URL).json()["data"]["items"] == []
    assert _delete(admin, row).status_code == 404
    with page_service.session_factory() as session:
        asset = session.get(ContextMediaAsset, (wid, UUID(row["asset_id"])))
        assert asset.association_state == "tombstoned"
        assert (
            session.scalar(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == wid,
                    AuditEvent.action == "admin.context_media.deleted",
                    AuditEvent.target_id == UUID(row["asset_id"]),
                )
            )
            is not None
        )


def test_used_and_open_edit_assets_cannot_be_cleaned_by_operator(page_service):
    admin = make_server_admin_client(page_service, "protected-operator@example.com")
    actor = _user_id(page_service, "protected-operator@example.com")
    wid = _workspace(page_service.session_factory, "Protected")
    used = _seed(page_service, wid, actor)
    protected = _seed(page_service, wid, actor, editing=True)
    with page_service.session_factory.begin() as session:
        session.add(
            ContextMediaUsage(
                workspace_id=wid,
                page_id=UUID(used["page_id"]),
                asset_id=UUID(used["asset_id"]),
                page_version_id=UUID(used["version_id"]),
                reference_count=1,
            )
        )
    rows = admin.get(URL).json()["data"]["items"]
    assert len(rows) == 1 and rows[0]["can_delete"] is False
    assert _delete(admin, rows[0]).status_code == 409
    with page_service.session_factory() as session:
        snapshot = SqlAlchemyContextMediaRepository(session).get_page_asset_usage(
            wid, UUID(protected["page_id"]), UUID(protected["asset_id"])
        )
        token = snapshot.usage_token
    assert (
        _delete(
            admin, {**protected, "workspace_id": str(wid), "usage_token": token}
        ).status_code
        == 409
    )


def test_real_shards_and_unavailable_shard_do_not_silently_truncate(tmp_path):
    control = _make_shard(tmp_path, "control")
    shards = {uuid4(): _make_shard(tmp_path, f"team-{i}") for i in range(2)}
    for i, (wid, factory) in enumerate(shards.items()):
        _register_reachable_workspace(
            control, factory, workspace_id=wid, slug=f"team-{i}"
        )
    provider = MultiShardProvider(control, shards)
    client = _build_admin_client(tmp_path, provider=provider, control_factory=control)
    with control() as session:
        from backend.models.user import User

        actor = str(
            session.scalar(
                select(User.id).where(User.email == "fanout-admin@example.com")
            )
        )
    expected = set()
    for wid, factory in shards.items():
        expected.add(
            _seed(SimpleNamespace(session_factory=factory), wid, actor)["asset_id"]
        )
    data = client.get(URL).json()["data"]
    assert {row["asset_id"] for row in data["items"]} == expected
    assert set(shards).issubset(provider.session_for_calls)
    row = data["items"][0]
    assert _delete(client, row).status_code == 200
    provider._poison_ids = frozenset(shards)
    assert client.get(URL).status_code == 503
    from sqlalchemy.exc import SQLAlchemyError

    with pytest.raises(SQLAlchemyError):
        list_admin_attachments(
            db=provider,
            repository_factory=SqlAlchemyContextMediaRepository,
            actor_id=UUID(actor),
        )


def test_admin_cursor_supports_long_unicode_search(page_service):
    admin = make_server_admin_client(page_service, "unicode-operator@example.com")
    actor = _user_id(page_service, "unicode-operator@example.com")
    wid = _workspace(page_service.session_factory, "Unicode")
    query = "😀" * 190
    for suffix in ("one", "two"):
        _seed(page_service, wid, actor, filename=query + suffix + ".png")
    first = admin.get(URL, params={"q": query, "limit": 1}).json()["data"]
    assert len(first["items"]) == 1
    assert len(first["next_cursor"]) < 2048
    second = admin.get(
        URL, params={"q": query, "limit": 1, "cursor": first["next_cursor"]}
    )
    assert second.status_code == 200
    assert (
        second.json()["data"]["items"][0]["asset_id"] != first["items"][0]["asset_id"]
    )
