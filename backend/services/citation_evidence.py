"""On-demand legacy evidence for one current published document relationship."""

import base64
import binascii
import hashlib
import hmac
import json
from uuid import UUID

from sqlalchemy import false, or_
from sqlalchemy.orm import load_only

from backend.models.agent import AgentPage
from backend.models.context import ContextCitation, ContextPage, ContextPageVersion
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.repositories.workspace_scoped import (
    join_in_workspace,
    select_in_workspace,
)
from backend.schemas.graph import (
    GraphCitationEvidenceItem,
    GraphCitationEvidencePayload,
)
from backend.services.actor_visible_read import (
    agent_page_visible,
    context_page_visible,
    library_item_visible,
)
from backend.services.citation_visibility import context_visibility_options
from backend.services.document_references import legacy_citation_owner_condition
from backend.services.library_canonical import get_canonical_library_version
from backend.services.read_errors import ReadNotFoundError, ReadValidationError


def _encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decoded(value):
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )


class CitationEvidenceService:
    def __init__(self, *, cursor_secret, read_service):
        self.secret = cursor_secret.encode()
        self.read_service = read_service

    def _cursor(self, scope, after):
        body = _encoded(
            json.dumps(
                {"v": 1, "scope": scope, "after": str(after)}, sort_keys=True
            ).encode()
        )
        signature = hmac.new(self.secret, body.encode(), hashlib.sha256).digest()
        return f"{body}.{_encoded(signature)}"

    def _after(self, token, scope):
        if token is None:
            return None
        try:
            body, signature = token.split(".")
            expected = hmac.new(self.secret, body.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _decoded(signature)):
                raise ValueError("signature")
            payload = json.loads(_decoded(body))
            if (
                not isinstance(payload, dict)
                or payload.get("v") != 1
                or payload.get("scope") != scope
            ):
                raise ValueError("scope")
            return UUID(payload["after"])
        except (ValueError, KeyError, TypeError, binascii.Error) as error:
            raise ReadValidationError(
                "citation evidence cursor is invalid or stale"
            ) from error

    def query(self, session, *, workspace_id, request, actor):
        source = session.scalar(
            select_in_workspace(
                ContextPage,
                workspace_id=workspace_id,
                conditions=(ContextPage.id == request.source_page_id,),
            ).options(*context_visibility_options())
        )
        if (
            not context_page_visible(source, actor, workspace_id=workspace_id)
            or not source.current_published_version_id
        ):
            raise ReadNotFoundError("citation relationship not found")
        version = session.scalar(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                conditions=(
                    ContextPageVersion.id == source.current_published_version_id,
                ),
            ).options(
                load_only(
                    ContextPageVersion.id,
                    ContextPageVersion.page_id,
                    ContextPageVersion.workspace_id,
                    ContextPageVersion.version_status,
                )
            )
        )
        if (
            version is None
            or version.page_id != source.id
            or version.version_status != "published_snapshot"
        ):
            raise ReadNotFoundError("citation relationship not found")
        self._require_target(session, workspace_id, request, actor)
        scope = hashlib.sha256(
            json.dumps(
                {
                    "workspace": str(workspace_id),
                    "source": str(source.id),
                    "version": str(version.id),
                    "target_kind": request.target_kind,
                    "target": str(request.target_id),
                    "limit": request.limit,
                    "actor": {
                        key: str(getattr(actor, key, None))
                        for key in (
                            "actor_kind",
                            "actor_id",
                            "role",
                            "workspace_role",
                            "token_kind",
                            "token_id",
                        )
                    },
                    "scopes": sorted(getattr(actor, "scopes", ())),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        after = self._after(request.cursor, scope)
        statement = select_in_workspace(
            ContextCitation,
            workspace_id=workspace_id,
            conditions=(
                legacy_citation_owner_condition(session, version, page=source),
                self._target_condition(workspace_id, request),
                *((ContextCitation.id > after,) if after is not None else ()),
            ),
        )
        # Bound inspected rows as well as returned records. Hidden evidence
        # never causes an unbounded scan to fill a page.
        rows = session.scalars(statement.order_by(ContextCitation.id).limit(201)).all()
        page = rows[:200]
        visible = self.read_service.citation_visibility_map(
            session, workspace_id, page, actor
        )
        readable = [row for row in page if visible.get(row.id)]
        selected = readable[: request.limit]
        last = (
            selected[-1].id
            if len(readable) > request.limit
            else page[-1].id
            if len(rows) > len(page)
            else None
        )
        # This is a bounded inspected window, not an assertion that filtered
        # evidence does not exist. An opaque source-owned continuation can
        # accompany an empty items list so later readable rows remain reachable.
        return GraphCitationEvidencePayload(
            source_page_id=source.id,
            source_version_id=version.id,
            target_kind=request.target_kind,
            target_id=request.target_id,
            items=[
                GraphCitationEvidenceItem(
                    id=row.id,
                    source_version_id=version.id,
                    owner_kind=row.target_kind,
                    owner_id=row.target_id,
                    citation_type=row.citation_type,
                    library_item_version_id=row.library_item_version_id,
                    library_artifact_id=row.library_artifact_id,
                    cited_context_page_version_id=row.cited_context_page_version_id,
                    location=row.location,
                    snippet=row.snippet,
                    verification_status=row.verification_status,
                    created_via=row.created_via,
                )
                for row in selected
            ],
            next_cursor=self._cursor(scope, last) if last is not None else None,
        )

    @staticmethod
    def _require_target(session, workspace_id, request, actor):
        model = {
            "context_page": ContextPage,
            "library_item": LibraryItem,
            "agent_page": AgentPage,
        }[request.target_kind]
        statement = select_in_workspace(
            model,
            workspace_id=workspace_id,
            conditions=(model.id == request.target_id,),
        )
        if model is ContextPage:
            statement = statement.options(*context_visibility_options())
        target = session.scalar(statement)
        if model is ContextPage:
            allowed = (
                context_page_visible(target, actor, workspace_id=workspace_id)
                and target.current_published_version_id is not None
            )
        elif model is LibraryItem:
            pointer = (
                target.current_ready_version_id or target.current_version_id
                if target
                else None
            )
            canonical_candidate = None
            if pointer is not None:
                # Preload only the canonical selector's metadata into the
                # identity map; its ordinary lookup must not hydrate raw text.
                canonical_candidate = session.scalar(
                    select_in_workspace(
                        LibraryItemVersion,
                        workspace_id=workspace_id,
                        conditions=(LibraryItemVersion.id == pointer,),
                    ).options(
                        load_only(
                            LibraryItemVersion.id,
                            LibraryItemVersion.library_item_id,
                            LibraryItemVersion.processing_status,
                        )
                    )
                )
            allowed = (
                library_item_visible(target, actor, workspace_id=workspace_id)
                and canonical_candidate is not None
                and (
                    get_canonical_library_version(session, target)
                    is canonical_candidate
                )
            )
        else:
            allowed = (
                agent_page_visible(target, actor, workspace_id=workspace_id)
                and target.current_active_version_id is not None
            )
        if not allowed:
            raise ReadNotFoundError("citation relationship not found")

    @staticmethod
    def _target_condition(workspace_id, request):
        if request.target_kind == "context_page":
            versions = select_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                conditions=(ContextPageVersion.page_id == request.target_id,),
            ).with_only_columns(ContextPageVersion.id)
            return ContextCitation.cited_context_page_version_id.in_(versions)
        if request.target_kind == "library_item":
            versions = select_in_workspace(
                LibraryItemVersion,
                workspace_id=workspace_id,
                conditions=(LibraryItemVersion.library_item_id == request.target_id,),
            ).with_only_columns(LibraryItemVersion.id)
            artifacts = join_in_workspace(
                select_in_workspace(LibraryArtifact, workspace_id=workspace_id),
                LibraryArtifact,
                LibraryItemVersion,
                workspace_id=workspace_id,
                on=(LibraryArtifact.library_item_version_id == LibraryItemVersion.id)
                & (LibraryItemVersion.library_item_id == request.target_id),
            ).with_only_columns(LibraryArtifact.id)
            return or_(
                ContextCitation.library_item_version_id.in_(versions),
                ContextCitation.library_artifact_id.in_(artifacts),
            )
        return false()
