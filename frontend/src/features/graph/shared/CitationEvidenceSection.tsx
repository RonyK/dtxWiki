import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { useOptionalAuth } from "../../../shared/auth/AuthContext";
import { ApiError } from "../../../shared/api/client";
import { queryGraphCitationEvidence, type GraphCitationEvidencePayload, type GraphCitationEvidenceRequest } from "../../../shared/api/graph";
import { humanizeEnum } from "../../../shared/lib/domainLabel";

export type CitationPair = Pick<GraphCitationEvidenceRequest, "source_page_id" | "target_kind" | "target_id">;
type EvidenceState = {
  scope: string;
  data: GraphCitationEvidencePayload | null;
  loading: boolean;
  error: boolean;
};

/** Exact legacy evidence is fetched only for the selected document relation. */
export function CitationEvidenceSection({ active = true, pair, revision, workspaceId }: {
  active?: boolean;
  pair: CitationPair;
  revision: string | number;
  workspaceId: string;
}) {
  const { t } = useTranslation("graph");
  const actorId = useOptionalAuth()?.user?.user_id ?? null;
  const headingId = useId();
  const scope = JSON.stringify([active, actorId, workspaceId, pair.source_page_id, pair.target_kind, pair.target_id, revision]);
  const liveScope = useRef(scope);
  useLayoutEffect(() => { liveScope.current = scope; }, [scope]);
  const controllers = useRef(new Set<AbortController>());
  const [state, setState] = useState<EvidenceState>({ scope: "", data: null, loading: false, error: false });
  const current = state.scope === scope ? state : null;
  const load = useCallback(async (cursor: string | null = null) => {
    if (!active || liveScope.current !== scope) return;
    const controller = new AbortController();
    controllers.current.add(controller);
    setState((previous) => ({ scope, data: cursor && previous.scope === scope ? previous.data : null, loading: true, error: false }));
    try {
      const data = await queryGraphCitationEvidence(workspaceId, { source_page_id: pair.source_page_id, target_kind: pair.target_kind, target_id: pair.target_id, cursor, limit: 20 }, controller.signal);
      if (controller.signal.aborted || liveScope.current !== scope) return;
      if (data.source_page_id !== pair.source_page_id || data.target_kind !== pair.target_kind || data.target_id !== pair.target_id) throw new Error("Mismatched citation evidence response");
      setState((previous) => {
        if (liveScope.current !== scope) return previous;
        if (cursor && previous.data?.source_version_id !== data.source_version_id) return { scope, data: null, loading: false, error: true };
        const items = cursor && previous.scope === scope
          ? [...new Map([...(previous.data?.items ?? []), ...data.items].map((item) => [item.id, item])).values()]
          : data.items;
        return { scope, data: { ...data, items }, loading: false, error: false };
      });
    } catch (cause) {
      if (!controller.signal.aborted && liveScope.current === scope) {
        const retain = !(cause instanceof ApiError && cause.status >= 400 && cause.status < 500);
        setState((previous) => ({ scope, data: retain && previous.scope === scope ? previous.data : null, loading: false, error: true }));
      }
    } finally {
      controllers.current.delete(controller);
    }
  }, [active, pair.source_page_id, pair.target_kind, pair.target_id, scope, workspaceId]);
  useEffect(() => {
    const pending = controllers.current;
    void load();
    return () => { pending.forEach((controller) => controller.abort()); pending.clear(); };
  }, [load]);
  if (!active) return null;
  return (
    <section aria-labelledby={headingId} className="graph-citation-evidence">
      <h5 id={headingId}>{t("graph.citationEvidence.title")}</h5>
      <p>{t("graph.citationEvidence.currentPublished")}</p>
      {current?.data ? <p>{t("graph.citationEvidence.sourceVersion")}: <code>{current.data.source_version_id}</code></p> : null}
      {!current || current.loading ? <p role="status">{t("graph.citationEvidence.loading")}</p> : null}
      {current?.error ? <><p role="alert">{t("graph.citationEvidence.failed")}</p>
        <button aria-label={t("graph.citationEvidence.retry")} className="icon-button text-button" disabled={current.loading} onClick={() => { void load(current.data?.next_cursor ?? null); }} type="button">{t("shell:actions.retry")}</button></> : null}
      {current?.data && !current.data.items.length && !current.data.next_cursor && !current.loading ? <p>{t("graph.citationEvidence.empty")}</p> : null}
      <ol>{current?.data?.items.map((item) => <li key={item.id}>
        <dl>
          <EvidenceField label={t("graph.citationEvidence.type")} value={humanizeEnum(item.citation_type)} />
          <EvidenceField label={t("graph.citationEvidence.status")} value={humanizeEnum(item.verification_status)} />
          <EvidenceField label={t("graph.citationEvidence.contextVersion")} value={item.cited_context_page_version_id} />
          <EvidenceField label={t("graph.citationEvidence.libraryVersion")} value={item.library_item_version_id} />
          <EvidenceField label={t("graph.citationEvidence.artifact")} value={item.library_artifact_id} />
          <EvidenceField label={t("graph.citationEvidence.location")} value={item.location ? JSON.stringify(item.location) : null} />
          <EvidenceField label={t("graph.citationEvidence.createdVia")} value={humanizeEnum(item.created_via)} />
        </dl>
        {item.snippet ? <blockquote>{item.snippet}</blockquote> : null}
      </li>)}</ol>
      {current?.data?.next_cursor && !current.error ? <button className="icon-button text-button" disabled={current.loading} onClick={() => { void load(current.data!.next_cursor); }} type="button">{t("graph.citationEvidence.loadMore")}</button> : null}
    </section>
  );
}

function EvidenceField({ label, value }: { label: string; value?: string | null }) {
  return value ? <div><dt>{label}</dt><dd>{value}</dd></div> : null;
}
