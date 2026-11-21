import { ChevronDown } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  getContextMediaAssetUsage,
  type ContextMediaAssetUsageData,
} from "../api/contextMedia";
import { contextHistoryPresentation } from "../lib/contextHistory";
import { formatVersionTimestamp } from "../lib/datetime";
import { contextPageHref, VERSION_QUERY_PARAM } from "../lib/workspaceRoutes";
import { StatusPill } from "./StatusPill";

/** Some version the viewer cannot read also uses the file. */
export function ContextMediaUnreadableUsagePill() {
  const { t } = useTranslation("pages");
  return (
    <StatusPill
      compact
      label={t("mediaManager.usageDetails.unreadableBadge")}
      status="info"
      title={t("mediaManager.usageDetails.unreadableHint")}
    />
  );
}

type HistoryProps = {
  workspaceId: string;
  pageId: string;
  assetId: string;
};

/**
 * The versions that use one attachment, fetched only when first expanded.
 * Only versions the viewer can read are listed; the response says when
 * others exist.
 */
export function ContextMediaUsageHistory(props: HistoryProps) {
  // Remount the request state whenever the attachment identity changes.
  return (
    <UsageHistory
      key={`${props.workspaceId}:${props.pageId}:${props.assetId}`}
      {...props}
    />
  );
}

function UsageHistory({ workspaceId, pageId, assetId }: HistoryProps) {
  const { t } = useTranslation("pages");
  const id = useId();
  const [expanded, setExpanded] = useState(false);
  const [data, setData] = useState<ContextMediaAssetUsageData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const generation = useRef(0);
  const pending = useRef(false);
  // A response that arrives after unmount is dropped.
  useEffect(
    () => () => {
      generation.current += 1;
    },
    [],
  );

  async function load(cursor?: string) {
    if (pending.current) return;
    pending.current = true;
    const request = ++generation.current;
    setLoading(true);
    setError(false);
    try {
      const result = await getContextMediaAssetUsage(workspaceId, assetId, cursor);
      if (generation.current !== request) return;
      setData((previous) => ({
        ...result,
        items: cursor
          ? [...(previous?.items ?? []), ...result.items]
          : result.items,
      }));
    } catch {
      if (generation.current === request) setError(true);
    } finally {
      if (generation.current === request) {
        pending.current = false;
        setLoading(false);
      }
    }
  }

  return (
    <div className="context-media-usage-history">
      <button
        aria-controls={id}
        aria-expanded={expanded}
        className="context-media-usage-toggle"
        onClick={() => {
          setExpanded(!expanded);
          if (!expanded && !data && !error) void load();
        }}
        type="button"
      >
        {t("mediaManager.usageDetails.toggle")}
        <ChevronDown aria-hidden="true" size={14} />
      </button>
      <div className="context-media-usage-panel" hidden={!expanded} id={id}>
        {loading ? (
          <p className="context-media-usage-note" role="status">
            {t("mediaManager.usageDetails.loading")}
          </p>
        ) : null}
        {error ? (
          <div className="context-media-usage-note" role="alert">
            <span>{t("mediaManager.usageDetails.error")}</span>
            <button
              className="text-button"
              onClick={() => void load(data?.next_cursor ?? undefined)}
              type="button"
            >
              {t("shell:actions.retry")}
            </button>
          </div>
        ) : null}
        {data ? (
          <>
            {data.items.length ? (
              <ul className="context-media-usage-rows">
                {data.items.map((item) => {
                  const timestamp = item.last_saved_at ?? item.created_at;
                  return (
                    <li key={item.version_id}>
                      <span className="context-media-usage-status">
                        {t(contextHistoryPresentation(item.history_status, item.closed_reason).labelKey)}
                      </span>
                      <span className="context-media-usage-version">
                        v{item.version_no}
                      </span>
                      <span className="context-media-usage-count">
                        {t("mediaManager.usageDetails.references", {
                          count: item.reference_count,
                        })}
                      </span>
                      <time className="context-media-usage-time" dateTime={timestamp}>
                        {formatVersionTimestamp(timestamp)}
                      </time>
                      <Link
                        className="context-media-usage-open"
                        to={`${contextPageHref(workspaceId, pageId)}?${VERSION_QUERY_PARAM}=${encodeURIComponent(item.version_id)}`}
                      >
                        {t("mediaManager.usageDetails.openVersion")}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="context-media-usage-note">
                {data.has_unreadable_usage
                  ? t("mediaManager.usageDetails.unreadableEmpty")
                  : t("mediaManager.usageDetails.empty")}
              </p>
            )}
            {data.items.length > 0 && data.has_unreadable_usage ? (
              <p className="context-media-usage-note">
                {t("mediaManager.usageDetails.unreadableNote")}
              </p>
            ) : null}
            {data.next_cursor ? (
              <button
                className="text-button context-media-usage-more"
                disabled={loading}
                onClick={() => void load(data.next_cursor ?? undefined)}
                type="button"
              >
                {t("mediaManager.usageDetails.more")}
              </button>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}
