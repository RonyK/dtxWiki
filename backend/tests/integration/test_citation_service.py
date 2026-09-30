from sqlalchemy import event, select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.context_write import CiteRequest, WriteDraftRequest
from backend.services import citation_service as citation_service_module
from backend.services.citation_service import CitationService
from backend.services.context_write_service import ContextWriteService
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
CITATION_ACTOR = ActorContext(
    actor_kind="system",
    actor_id="citation-service-test",
    scopes=("*",),
    workspace_role="owner",
    role="owner",
    token_kind="system",
)


def _workspace(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


def test_citation_service_returns_library_artifact_candidates(
    session_factory, media_installer_factory
):
    service = CitationService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/citation-source",
            title="Citation Source",
            format_hint="markdown",
            raw_text="# Vector Search\n\npgvector supports vector search indexes.",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="pgvector supports vector search indexes",
                desired_citation_type="raw_source",
            ),
        )

    assert result.citation_candidates
    candidate = result.citation_candidates[0]
    assert candidate.citation_type == "derived_artifact"
    assert candidate.library_item_version_id is None
    assert candidate.library_artifact_id == artifact.id
    assert candidate.cited_context_page_version_id is None
    assert candidate.support_level in {"strong", "partial"}
    assert candidate.recommendation == "use"


def test_citation_service_keeps_ready_artifact_during_new_pending_submission(
    session_factory, media_installer_factory,
):
    service = CitationService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        ready = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/cite-ready-pending",
            title="Ready while pending",
            raw_text="durablereadymarker",
        )
        ready_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=ready.version.id,
        )
        ready.library_item.current_ready_version_id = ready.version.id

        pending = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/cite-ready-pending",
            title="Ready while pending",
            raw_text="pendingreplacementmarker",
        )
        assert pending.version.processing_status == "pending"
        assert pending.library_item.current_version_id == pending.version.id

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="durablereadymarker",
                desired_citation_type="raw_source",
            ),
        )

    assert [
        candidate.library_artifact_id for candidate in result.citation_candidates
    ] == [ready_artifact.id]


def test_citation_service_filters_restricted_archived_and_stale_sources(
    session_factory, media_installer_factory,
):
    service = CitationService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        visible = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/cite-visible",
            title="Visible citation source",
            raw_text="citation-visibility marker visible",
        )
        visible_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=visible.version.id,
        )
        restricted = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/cite-restricted",
            title="Restricted citation source",
            trust_level="restricted",
            raw_text="citation-visibility marker restricted",
        )
        restricted_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=restricted.version.id,
        )
        archived = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/cite-archived",
            title="Archived citation source",
            raw_text="citation-visibility marker archived",
        )
        archived_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=archived.version.id,
        )
        archived.library_item.lifecycle_status = "archived"

        stale = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/cite-versioned",
            title="Versioned citation source",
            raw_text="citation-visibility marker old",
        )
        stale_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=stale.version.id,
        )
        current = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/cite-versioned",
            title="Versioned citation source",
            raw_text="citation-visibility marker current",
        )
        current_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=current.version.id,
        )
        editor = ActorContext(
            actor_kind="mcp_client",
            actor_id="citation-editor",
            scopes=("mcp:write", "cite"),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
            token_kind="mcp_client",
        )

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=editor,
            request=CiteRequest(
                claim_text="citation-visibility marker",
                desired_citation_type="raw_source",
                limit=20,
            ),
        )
        owner_result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="citation-visibility marker restricted",
                desired_citation_type="raw_source",
                source_scope=[restricted.library_item.id],
                limit=20,
            ),
        )

    candidate_ids = {
        candidate.library_artifact_id for candidate in result.citation_candidates
    }
    assert visible_artifact.id in candidate_ids
    assert current_artifact.id in candidate_ids
    assert restricted_artifact.id not in candidate_ids
    assert archived_artifact.id not in candidate_ids
    assert stale_artifact.id not in candidate_ids
    assert [
        candidate.library_artifact_id for candidate in owner_result.citation_candidates
    ] == [restricted_artifact.id]


def test_citation_service_excludes_hidden_library_items(
    session_factory, media_installer_factory
):
    # M-04: a hidden-lifecycle Library item is excluded from every other
    # retrieval lane; cite must not surface it as a candidate either.
    service = CitationService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        visible = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/m04-visible",
            title="M04 visible source",
            raw_text="m04-hidden-marker visible",
        )
        visible_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=visible.version.id,
        )
        hidden = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/m04-hidden",
            title="M04 hidden source",
            raw_text="m04-hidden-marker hidden",
        )
        hidden_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=hidden.version.id,
        )
        hidden.library_item.lifecycle_status = "hidden"
        session.flush()

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="m04-hidden-marker",
                desired_citation_type="raw_source",
                limit=20,
            ),
        )

    ids = {candidate.library_artifact_id for candidate in result.citation_candidates}
    assert visible_artifact.id in ids
    assert hidden_artifact.id not in ids


def test_citation_service_prefers_published_over_newer_draft(session_factory):
    # M-04: for a page with a published version AND a newer draft, an editor's
    # cite must target the PUBLISHED version (stable, viewer-readable), not the
    # draft that a viewer cannot read and that dead-ends if discarded.
    service = CitationService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="m04-pub-draft",
            title="M04 Published With Draft",
            page_type="concept",
            lifecycle_status="published",
        )
        session.add(page)
        session.flush()
        published = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=1,
            body_md="# M04\n\nm04-marker published body.",
            content_hash="m04-pub-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
            version_status="published_snapshot",
        )
        draft = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=2,
            body_md="# M04\n\nm04-marker draft body.",
            content_hash="m04-draft-v2",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
            version_status="draft",
        )
        session.add_all([published, draft])
        session.flush()
        page.current_published_version_id = published.id
        page.current_version_id = draft.id
        page.latest_draft_version_id = draft.id
        page_id = page.id
        published_id = published.id
        draft_id = draft.id

        editor = ActorContext(
            actor_kind="mcp_client",
            actor_id="m04-editor",
            scopes=("mcp:write", "cite"),
            workspace_id=workspace.id,
            workspace_role="editor",
            role="editor",
            token_kind="mcp_client",
        )
        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=editor,
            request=CiteRequest(
                claim_text="m04-marker",
                desired_citation_type="context_page",
                limit=20,
            ),
        )

    matches = [
        candidate
        for candidate in result.citation_candidates
        if candidate.citation_type == "context_page"
        and candidate.location.get("page_id") == str(page_id)
    ]
    assert matches, "the editor can see the page and should get a candidate"
    assert matches[0].cited_context_page_version_id == published_id
    assert matches[0].cited_context_page_version_id != draft_id


def test_citation_service_returns_context_page_candidates(session_factory):
    service = CitationService()
    write_service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        draft = write_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Context Citation Source",
                page_type="concept",
                body_markdown=(
                    "# Context Citation Source\n\n"
                    "Citation candidates may come from context pages."
                ),
                reason="create context citation source",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="citation candidates from context pages",
                desired_citation_type="context_page",
            ),
        )

    candidate = result.citation_candidates[0]
    assert candidate.citation_type == "context_page"
    assert candidate.cited_context_page_version_id == draft.draft_version_id
    assert candidate.library_item_version_id is None
    # Codex #15: this page has no transitive raw-source lineage, so the
    # recommender must flag it as a provenance dead-end and never recommend
    # it as 'use'.
    assert candidate.recommendation == "needs_review"
    assert candidate.location.get("provenance_dead_end") is True


def test_citation_service_uses_context_page_body_md_as_canonical(session_factory):
    service = CitationService()
    write_service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        draft = write_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Canonical Body Source",
                page_type="concept",
                body_markdown="Stale mirror text only.",
                reason="create divergent context citation source",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        version = session.get(ContextPageVersion, draft.draft_version_id)
        assert version is not None
        version.body_markdown = "Canonical citation body mentions alias evidence."
        assert version.body_md == "Canonical citation body mentions alias evidence."

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="alias evidence",
                desired_citation_type="context_page",
            ),
        )

    assert result.citation_candidates
    assert result.citation_candidates[0].cited_context_page_version_id == (
        draft.draft_version_id
    )


def test_citation_service_honors_limit_and_source_scope(
    session_factory, media_installer_factory
):
    service = CitationService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        first = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/scoped-source-1",
            title="Scoped Source 1",
            raw_text="Scoped evidence alpha.",
        )
        second = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/scoped-source-2",
            title="Scoped Source 2",
            raw_text="Scoped evidence beta.",
        )
        ExtractionService(media_installer_factory=media_installer_factory).extract_library_item_version(
            session=session,
            library_item_version_id=first.version.id,
        )
        second_artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=second.version.id,
        )

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="scoped evidence",
                source_scope=[second.library_item.id],
                limit=1,
            ),
        )

    assert len(result.citation_candidates) == 1
    assert result.citation_candidates[0].library_artifact_id == second_artifact.id


def test_citation_service_scores_cjk_claim_terms(
    session_factory, media_installer_factory
):
    service = CitationService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/korean-source",
            title="한국어 검색 문서",
            format_hint="markdown",
            raw_text="벡터 검색 색인은 한국어 근거 문서를 찾는 데 사용된다.",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="한국어 근거 검색",
                desired_citation_type="raw_source",
            ),
        )

    assert result.citation_candidates
    assert result.citation_candidates[0].library_artifact_id == artifact.id
    assert result.citation_candidates[0].recommendation == "use"


def test_citation_service_scores_no_whitespace_cjk_terms(
    session_factory, media_installer_factory
):
    service = CitationService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/korean-source-no-space",
            title="한국어붙임검색",
            format_hint="markdown",
            raw_text="벡터검색색인은한국어근거문서를찾는데사용된다.",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="한국어근거검색",
                desired_citation_type="raw_source",
            ),
        )

    assert result.citation_candidates
    assert result.citation_candidates[0].library_artifact_id == artifact.id
    assert result.citation_candidates[0].recommendation == "use"


def test_citation_service_caps_source_scan(
    session_factory, monkeypatch, media_installer_factory
):
    monkeypatch.setattr(citation_service_module, "CITATION_SCAN_LIMIT", 2)
    service = CitationService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        artifact_ids = []
        for title in ["A Scan Source", "B Scan Source", "C Scan Source"]:
            registration = LibraryService().register_library_item(
                session=session,
                workspace_id=workspace.id,
                canonical_uri=f"https://example.com/{title.lower().replace(' ', '-')}",
                title=title,
                raw_text="scan-limit-token supporting source evidence",
            )
            artifact = ExtractionService(
                media_installer_factory=media_installer_factory
            ).extract_library_item_version(
                session=session,
                library_item_version_id=registration.version.id,
            )
            artifact_ids.append(artifact.id)

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="scan-limit-token",
                desired_citation_type="raw_source",
                limit=10,
            ),
        )

    library_artifact_ids = [
        candidate.library_artifact_id for candidate in result.citation_candidates
    ]
    assert library_artifact_ids == artifact_ids[:2]


def test_citation_service_caps_context_page_scan(session_factory, monkeypatch):
    monkeypatch.setattr(citation_service_module, "CITATION_SCAN_LIMIT", 2)
    service = CitationService()
    write_service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        version_ids = []
        for title in ["A Scan Context", "B Scan Context", "C Scan Context"]:
            draft = write_service.write_draft(
                session=session,
                workspace_id=workspace.id,
                request=WriteDraftRequest(
                    title=title,
                    page_type="concept",
                    body_markdown="scan-limit-token supporting context evidence",
                    reason="create scan cap context page",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )
            version_ids.append(draft.draft_version_id)

        result = service.cite(
            session=session,
            workspace_id=workspace.id,
            actor=CITATION_ACTOR,
            request=CiteRequest(
                claim_text="scan-limit-token",
                desired_citation_type="context_page",
                limit=10,
            ),
        )

    assert [
        candidate.cited_context_page_version_id
        for candidate in result.citation_candidates
    ] == version_ids[:2]


def test_citation_scoring_caps_candidate_text_work(monkeypatch):
    monkeypatch.setattr(citation_service_module, "CITATION_SCORE_TEXT_MAX_CHARS", 20)

    assert citation_service_module.score_text("needle", "needle before cap") > 0
    assert citation_service_module.score_text("needle", "x" * 40 + " needle") == 0


def test_citation_service_loads_context_page_versions_in_one_query(
    session_factory,
):
    service = CitationService()
    write_service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        for index in range(3):
            write_service.write_draft(
                session=session,
                workspace_id=workspace.id,
                request=WriteDraftRequest(
                    title=f"Joined Context {index}",
                    page_type="concept",
                    body_markdown="join-token supporting context evidence",
                    reason="create joined context page",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )
        workspace_id = workspace.id

    engine = session_factory.kw["bind"]
    version_selects: list[str] = []

    def capture_version_select(
        conn,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        del conn, cursor, parameters, context, executemany
        normalized = statement.lower()
        if (
            normalized.lstrip().startswith("select")
            and "context_page_version" in normalized
            # Count only the candidate scan (FROM context_page JOIN
            # context_page_version), not the bounded #15 provenance-lineage
            # queries (FROM context_citation, which mention the
            # cited_context_page_version_id column).
            and "from context_page" in normalized
        ):
            version_selects.append(statement)

    event.listen(engine, "before_cursor_execute", capture_version_select)
    try:
        with session_factory.begin() as session:
            result = service.cite(
                session=session,
                workspace_id=workspace_id,
                actor=CITATION_ACTOR,
                request=CiteRequest(
                    claim_text="join-token",
                    desired_citation_type="context_page",
                    limit=10,
                ),
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_version_select)

    assert len(result.citation_candidates) == 3
    assert len(version_selects) == 1
