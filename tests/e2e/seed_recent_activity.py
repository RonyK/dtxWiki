"""Isolated browser fixtures for the real typed activity feed.

Only the database allocated by run-e2e.mjs is accepted. These rows model four
successful reads of one immutable version, a failed write, two audits and a
pending review. Capture-at-serve behavior has separate integration coverage.
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import make_engine
from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.mcp_chat import McpChatSession, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.models.review import ReviewItem
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.services.mcp_chat_service import mcp_session_trace_uuid
from backend.services.tree_service import ensure_primary_tree_placement

# As long as a display name may be (DISPLAY_NAME_MAX_LENGTH = 32).
USER_NAME = "Documentation Reviewer Avery Kim"
MCP_NAME = "Codex CLI — Product documentation workspace and integration verification"
PAGE_TITLE = "Activity navigation and repeated reads guide"
REVIEW_TITLE = "Activity UI changes awaiting review"


def main() -> None:
    database_url = os.environ.get("DTXWIKI_E2E_DATABASE_URL", "")
    database_path = os.environ.get("DTXWIKI_E2E_DATABASE_PATH", "")
    if not database_path or "dtxwiki-e2e-" not in database_path:
        raise RuntimeError("Use npm run test:e2e with its isolated database")
    if database_url != f"sqlite+pysqlite:///{database_path.replace(chr(92), '/')}":
        raise RuntimeError("The E2E database URL must match the isolated path")
    workspace_id, email = UUID(sys.argv[1]), sys.argv[2]
    engine = make_engine(database_url)
    with Session(engine) as session, session.begin():
        workspace = session.get(Workspace, workspace_id)
        user = session.scalar(select(User).where(User.email == email))
        if workspace is None or user is None:
            raise RuntimeError("Create the browser user and workspace first")
        user.display_name = USER_NAME
        now = datetime.now(UTC)
        client = MCPClient(
            workspace_id=workspace_id,
            name=MCP_NAME,
            issued_to_user_id=user.id,
        )
        page = ContextPage(
            workspace_id=workspace_id,
            slug=f"activity-browser-{uuid4().hex}",
            title=PAGE_TITLE,
            page_type="overview",
            lifecycle_status="published",
        )
        session.add_all([client, page])
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=1,
            body_md=f"# {PAGE_TITLE}\n\nBrowser activity target.",
            content_hash="a" * 64,
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=str(user.id),
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        page.current_published_version_id = version.id
        ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace_id,
            target_kind="context_page",
            target_id=page.id,
            title=page.title,
            section="context",
            parent_id=None,
            default_parent_slug="inbox",
            actor_kind="human",
            actor_id=str(user.id),
        )
        chat = McpChatSession(
            workspace_id=workspace_id,
            client_id=client.id,
            owner_user_id=user.id,
            mcp_session_id=f"conv_{uuid4().hex}",
        )
        session.add(chat)
        session.flush()
        for index in range(5):
            failed = index == 0
            at = now - timedelta(seconds=20 if failed else index * 60)
            call = MCPToolCall(
                workspace_id=workspace_id,
                client_id=client.id,
                mcp_session_id=mcp_session_trace_uuid(chat.mcp_session_id),
                request_id=uuid4(),
                tool_name="update_context_edit" if failed else "read",
                result_status="conflict" if failed else "succeeded",
                error_code="version_conflict" if failed else None,
                input_summary={
                    "target_type": "context_page",
                    "target_id": str(page.id),
                },
                result_summary="Untrusted secret-sentinel must stay hidden",
                result_detail=None
                if failed
                else {
                    "read_projection": {
                        "schema": 1,
                        "resource_kind": "context_page",
                        "resource_id": str(page.id),
                        "version_id": str(version.id),
                        "version_status": "published_snapshot",
                        "content_identity": "a" * 64,
                        "scope_identity": "b" * 64,
                        "editing_revision": None,
                    }
                },
                started_at=at,
                completed_at=at,
                latency_ms=12,
            )
            session.add(call)
            session.flush()
            session.add(
                McpChatToolCallArtifact(
                    workspace_id=workspace_id,
                    chat_session_id=chat.id,
                    mcp_tool_call_id=call.id,
                    resource_kind="context_page",
                    resource_id=page.id,
                    verb="edited" if failed else "referenced",
                )
            )
        for index, action in enumerate(
            ["context_page.draft.update", "context_page.draft.publish"]
        ):
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_kind="human",
                    actor_type="human",
                    actor_id=str(user.id),
                    action=action,
                    target_kind="context_page",
                    target_type="context_page",
                    target_id=page.id,
                    created_at=now - timedelta(minutes=5 + index),
                    payload={},
                )
            )
        change = ChangeSet(
            workspace_id=workspace_id,
            trigger_type="manual",
            actor_type="human",
            actor_id=user.id,
            status="review_pending",
            reason="Browser review fixture",
        )
        session.add(change)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=workspace_id,
                change_set_id=change.id,
                target_type="context_page",
                target_id=page.id,
                operation="update",
                diff_summary="Activity link and spacing changes",
            )
        )
        review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change.id,
            review_type="context_page_draft",
            status="pending",
            risk_level="low",
            decision_summary=REVIEW_TITLE,
            created_at=now - timedelta(seconds=10),
        )
        session.add(review)
        session.flush()
        result = {
            "page_id": str(page.id),
            "review_id": str(review.id),
            "page_title": PAGE_TITLE,
            "review_title": REVIEW_TITLE,
            "user_name": USER_NAME,
            "mcp_name": MCP_NAME,
        }
    engine.dispose()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
