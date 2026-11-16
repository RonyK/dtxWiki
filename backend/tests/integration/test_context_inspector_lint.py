from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.models.base import Base
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.domain_read import DomainReadService
from backend.services.page_service import PageService
from backend.tests.utils.context_git import configure_test_context_storage
from backend.utils.content_hash import content_hash

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


def make_session_factory(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return configure_test_context_storage(
        sessionmaker(bind=engine, expire_on_commit=False), tmp_path / "repositories"
    )


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


def _inspector(
    read_service,
    *,
    session,
    workspace_id,
    page_id,
    actor,
    version_id=None,
    include_warnings=True,
):
    page = session.get(ContextPage, page_id)
    version = session.get(
        ContextPageVersion, version_id or page.current_published_version_id
    )
    if version is None:
        version = session.scalar(
            select(ContextPageVersion).where(ContextPageVersion.page_id == page_id)
        )
    return read_service.context_page_inspector(
        session,
        workspace_id,
        page,
        version,
        actor,
        section="summary" if include_warnings else "backlinks",
    )


def test_context_detail_surfaces_format_lint_warnings(tmp_path):
    # E-T3: a format issue (unclosed code fence) shows up as a non-blocking
    # warning on the page detail, which the Context inspector renders.
    session_factory = make_session_factory(tmp_path)
    read_service = DomainReadService()
    with session_factory.begin() as session:
        seed_default_workspace(session)
    page = _write(session_factory, slug="bad-fence", body=UNCLOSED_FENCE_BODY)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        detail = _inspector(
            read_service,
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            actor=_editor(workspace.id),
        )
        assert detail["warnings"]
        assert any("fence" in w.lower() for w in detail["warnings"])


def test_inspector_warnings_are_version_aware_and_skippable(tmp_path):
    # M1: warnings reflect the EXACT version read (version-aware), and the
    # internal existence-check path can skip the lint work entirely.
    session_factory = make_session_factory(tmp_path)
    read_service = DomainReadService(
        context_link_origins=frozenset({"https://docs.example"})
    )
    with session_factory.begin() as session:
        seed_default_workspace(session)
    # Budget exception: this adds one logical DB input for C2. The same editor
    # must resolve a configured-origin link to their own unpublished version;
    # format-only fixtures cannot detect lost actor/origin context, and a unit
    # wiring assertion cannot prove the real target-visibility decision.
    page = _write(session_factory, slug="ver", body=UNCLOSED_FENCE_BODY)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        # Seed a supported self-link once the page UUID is allocated, retaining
        # this reader fixture's configured-origin and unpublished-target checks.
        stored = session.get(ContextPageVersion, page.version_id)
        stored.body_md = (
            f"[This draft](https://docs.example/workspaces/{workspace.id}"
            f"/context/pages/{page.id})\n\n" + UNCLOSED_FENCE_BODY
        )
        stored.content_hash = content_hash(stored.body_md)
        explicit = _inspector(
            read_service,
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            version_id=page.version_id,
            actor=_editor(workspace.id),
        )
        assert any("fence" in w.lower() for w in explicit["warnings"])
        skipped = _inspector(
            read_service,
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            include_warnings=False,
            actor=_editor(workspace.id),
        )
        assert skipped["warnings"] == []


def test_clean_context_detail_has_no_format_warnings(tmp_path):
    session_factory = make_session_factory(tmp_path)
    read_service = DomainReadService()
    with session_factory.begin() as session:
        seed_default_workspace(session)
    page = _write(session_factory, slug="clean", body=CLEAN_BODY)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        detail = _inspector(
            read_service,
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            actor=_editor(workspace.id),
        )
        assert detail["warnings"] == []


def test_disabling_a_format_rule_suppresses_its_inspector_warning(tmp_path):
    session_factory = make_session_factory(tmp_path)
    read_service = DomainReadService()
    with session_factory.begin() as session:
        workspace_id = seed_default_workspace(session).id
    page = _write(session_factory, slug="bad-fence-off", body=UNCLOSED_FENCE_BODY)
    with session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        workspace.lint_policy = {"md.code_fence_balance": False}
    with session_factory.begin() as session:
        detail = _inspector(
            read_service,
            session=session,
            workspace_id=workspace_id,
            page_id=page.id,
            actor=_editor(workspace_id),
        )
        # The fence rule is a required publishing safeguard and refuses to be
        # switched off: hiding the finding while saves are still held by the same
        # check would read as a bug, not a policy.
        assert any("fence" in w.lower() for w in detail["warnings"])
