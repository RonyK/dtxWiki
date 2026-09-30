from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.base import Base
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.domain_read import DomainReadService
from backend.services.page_service import PageService

UNCLOSED_FENCE_BODY = "# Title\n\n```python\nx = 1\n"
CLEAN_BODY = "# Title\n\nA clean paragraph.\n"


def _editor(workspace_id=None):
    """A body with an unclosed fence is now held as a draft rather than
    published, so reading it back needs an actor who can see drafts. These tests
    are about which warnings the inspector surfaces, not about visibility."""
    return ActorContext(
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        workspace_role="editor",
        role="editor",
        workspace_id=workspace_id,
    )


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _write(session_factory, *, slug, body):
    return PageService(session_factory).write_page(
        workspace_slug="default",
        slug=slug,
        title=f"Title {slug}",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )


def test_context_detail_surfaces_format_lint_warnings():
    # E-T3: a format issue (unclosed code fence) shows up as a non-blocking
    # warning on the page detail, which the Context inspector renders.
    session_factory = make_session_factory()
    read_service = DomainReadService()
    with session_factory.begin() as session:
        seed_default_workspace(session)
    page = _write(session_factory, slug="bad-fence", body=UNCLOSED_FENCE_BODY)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            actor=_editor(workspace.id),
        )
        assert detail.warnings
        assert any("fence" in w.lower() for w in detail.warnings)


def test_inspector_warnings_are_version_aware_and_skippable():
    # M1: warnings reflect the EXACT version read (version-aware), and the
    # internal existence-check path can skip the lint work entirely.
    session_factory = make_session_factory()
    read_service = DomainReadService()
    with session_factory.begin() as session:
        seed_default_workspace(session)
    page = _write(session_factory, slug="ver", body=UNCLOSED_FENCE_BODY)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        explicit = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            version_id=page.version_id,
            actor=_editor(workspace.id),
        )
        assert any("fence" in w.lower() for w in explicit.warnings)
        skipped = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            include_warnings=False,
            actor=_editor(workspace.id),
        )
        assert skipped.warnings == []


def test_clean_context_detail_has_no_format_warnings():
    session_factory = make_session_factory()
    read_service = DomainReadService()
    with session_factory.begin() as session:
        seed_default_workspace(session)
    page = _write(session_factory, slug="clean", body=CLEAN_BODY)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            actor=_editor(workspace.id),
        )
        assert detail.warnings == []


def test_disabling_a_format_rule_suppresses_its_inspector_warning():
    session_factory = make_session_factory()
    read_service = DomainReadService()
    with session_factory.begin() as session:
        workspace_id = seed_default_workspace(session).id
    page = _write(session_factory, slug="bad-fence-off", body=UNCLOSED_FENCE_BODY)
    with session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        workspace.lint_policy = {"md.code_fence_balance": False}
    with session_factory.begin() as session:
        detail = read_service.read_context_page(
            session=session,
            workspace_id=workspace_id,
            page_id=page.id,
            actor=_editor(workspace_id),
        )
        # The fence rule is a required publishing safeguard and refuses to be
        # switched off: hiding the finding while saves are still held by the same
        # check would read as a bug, not a policy.
        assert any("fence" in w.lower() for w in detail.warnings)
