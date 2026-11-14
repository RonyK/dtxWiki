import { useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { listContextDrafts, type ContextPageDetail, type OpenDrafts } from "../../shared/api/context";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { TREE_VERSION_PARAM } from "../../shared/components/treeRouteState";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { workspacePath } from "../../shared/lib/workspaceRoutes";

export function ContextDrafts({ detail, workspaceId }: {
  detail: ContextPageDetail;
  workspaceId: string;
}) {
  const { t } = useTranslation("context");
  const [searchParams, setSearchParams] = useSearchParams();
  const [summary, setSummary] = useState<OpenDrafts | null>(detail.open_drafts ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const activeRef = useRef(true);
  const loadingRef = useRef(false);
  const generationRef = useRef(0);
  useLayoutEffect(() => {
    activeRef.current = true;
    return () => { activeRef.current = false; };
  }, []);
  useLayoutEffect(() => {
    generationRef.current += 1;
    loadingRef.current = false;
    setSummary(detail.open_drafts ?? null);
    setLoading(false);
    setError(false);
    return () => { generationRef.current += 1; };
  }, [detail.open_drafts]);

  function selectVersion(versionId: string | null) {
    const next = new URLSearchParams(searchParams);
    if (versionId) next.set(TREE_VERSION_PARAM, versionId);
    else next.delete(TREE_VERSION_PARAM);
    next.delete("tab");
    next.delete("compareFrom");
    next.delete("compareTo");
    next.delete("reconcile");
    setSearchParams(next);
  }

  async function loadDrafts(append: boolean) {
    const cursor = append ? summary?.next_cursor : undefined;
    if ((append && !cursor) || loadingRef.current) return;
    const generation = generationRef.current;
    const isCurrent = () => activeRef.current && generationRef.current === generation;
    loadingRef.current = true;
    setLoading(true);
    setError(false);
    try {
      const next = await listContextDrafts(workspaceId, detail.id, cursor ?? undefined);
      if (!isCurrent()) return;
      setSummary((current) => {
        if (!append) return next;
        const items = current?.items ?? [];
        const seen = new Set(items.map((item) => item.version_id));
        return {
          ...next,
          items: [...items, ...(next.items ?? []).filter((item) => !seen.has(item.version_id))],
        };
      });
    } catch {
      if (isCurrent()) setError(true);
    } finally {
      if (isCurrent()) {
        loadingRef.current = false;
        setLoading(false);
      }
    }
  }

  const draft = detail.draft;
  const author = draft?.author.display_name || t("drafts.unknownAuthor");
  return (
    <div className="context-drafts">
      {draft ? (
        <StatusBanner tone="info">
          <div className="context-draft-banner">
            <strong>{t("drafts.banner")}</strong>
            <span>{author}</span>
            <time dateTime={draft.last_saved_at}>
              {t("drafts.lastSaved", { time: formatVersionTimestamp(draft.last_saved_at) })}
            </time>
            {draft.is_editing ? <span>{t("drafts.writing")}</span> : null}
            {draft.review ? (
              <Link to={workspacePath(workspaceId, `review/${encodeURIComponent(draft.review.id)}`)}>
                <StatusPill compact status={draft.review.status} />
              </Link>
            ) : null}
            {draft.is_behind === true ? <span>{t("drafts.behind")}</span> : null}
            {detail.current_published_version_id ? (
              <button className="text-button" onClick={() => selectVersion(null)} type="button">
                {t("drafts.backPublished")}
              </button>
            ) : null}
          </div>
        </StatusBanner>
      ) : null}
      {summary ? (
        <details className="context-draft-list" onToggle={(event) => {
          if (event.currentTarget.open) void loadDrafts(false);
        }}>
          <summary>{t("drafts.count", { count: summary.total })}</summary>
          {(summary.items ?? []).length ? (
            <ul>
              {(summary.items ?? []).map((item) => (
                <li key={item.version_id}>
                  <button
                    aria-current={item.version_id === detail.version_info?.version_id ? "true" : undefined}
                    className="context-draft-choice text-button"
                    onClick={() => selectVersion(item.version_id)}
                    type="button"
                  >
                    <span>{item.author.display_name || t("drafts.unknownAuthor")}</span>
                    {item.is_mine ? <span>{t("drafts.mine")}</span> : null}
                    <span>{item.state === "reviewing" ? t("drafts.reviewing") : t("drafts.writing")}</span>
                    <time dateTime={item.last_saved_at}>
                      {t("drafts.lastSaved", { time: formatVersionTimestamp(item.last_saved_at) })}
                    </time>
                  </button>
                </li>
              ))}
            </ul>
          ) : <p>{t("drafts.empty")}</p>}
          {error ? <p role="alert">{t("drafts.loadError")}</p> : null}
          {summary.next_cursor ? (
            <button className="text-button" disabled={loading} onClick={() => void loadDrafts(true)} type="button">
              {loading ? t("drafts.loading") : t("drafts.more")}
            </button>
          ) : null}
        </details>
      ) : null}
    </div>
  );
}
