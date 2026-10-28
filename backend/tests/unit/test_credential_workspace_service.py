import pytest

from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.credential_workspace_service import list_credential_workspaces
from backend.tests.unit import test_mcp_access_db_consistency as fixtures

db = fixtures.db

def test_membership_only_search_cursor_order_and_permissions(db):
    with db.factory.begin() as session:
        for index in range(24):
            w = Workspace(
                slug=f"candidate-{index:02}", title=f"Candidate {index:02}", name="Test"
            )
            session.add(w)
            session.flush()
            if index != 23:
                session.add(
                    WorkspaceMembership(
                        user_id=db.user_id, workspace_id=w.id, role="viewer"
                    )
                )
            if index == 15:
                session.get(User, db.user_id).default_workspace_id = w.id
        session.flush()

    def read(**kwargs):
        with db.factory() as session:
            return list_credential_workspaces(
                session, user_id=db.user_id, secret="test", **kwargs
            )

    first = read()
    assert len(first["data"]) == 8
    assert first["data"][0]["title"] == "Candidate 15"
    assert first["total_count"] == 24
    ids = [r["id"] for r in first["data"]]
    cursor = first["next_cursor"]
    while cursor:
        page = read(cursor=cursor)
        ids.extend(r["id"] for r in page["data"])
        cursor = page["next_cursor"]
    assert len(ids) == len(set(ids)) == 24
    found = read(query="CANDIDATE-22")
    assert [r["slug"] for r in found["data"]] == ["candidate-22"]
    assert read(query="candidate-23")["data"] == []
    with pytest.raises(ValueError, match="cursor"):
        read(query="candidate", cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="cursor"):
        read(cursor=first["next_cursor"] + "bad")
    with db.factory.begin() as session:
        other = User(
            email="other@example.com", display_name="Other", password_hash="unused"
        )
        session.add(other)
        session.flush()
        other_id = other.id
    with db.factory() as session:
        with pytest.raises(ValueError, match="cursor"):
            list_credential_workspaces(
                session, user_id=other_id, secret="test", cursor=first["next_cursor"]
            )
