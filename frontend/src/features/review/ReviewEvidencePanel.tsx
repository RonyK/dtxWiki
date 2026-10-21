import { ExternalLink, FileText, RefreshCcw } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ReviewEvidenceOperation, ReviewItem } from "../../shared/api/operations";
import { ApiError } from "../../shared/api/client";
import {
  contextMediaPageVersionContentUrl,
  type ContextMediaAssetUrlResolver,
} from "../../shared/api/contextMedia";
import {
  diffPageVersions,
  readPage,
  type PagePayload,
  type PageVersionDiff,
} from "../../shared/api/pages";
import { DiffViewer } from "../../shared/components/DiffViewer";
import { ReaderTabs } from "../../shared/components/ReaderTabs";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { domainLabel } from "../../shared/lib/domainLabel";
import { MarkdownView } from "../pages/MarkdownView";
import { parsePageDocument } from "../pages/pageFrontmatter";

import "./review-content.css";

type DocumentTarget = NonNullable<ReviewEvidenceOperation["document"]>;
type ContentTarget = NonNullable<ReviewEvidenceOperation["content"]>;
type ContentStatus = "loading" | "ready" | "unavailable";
type ContentTab = "comparison" | "original";
type ReadFailure = "unavailable" | "too_large" | "mismatch" | "failed";
type ReadResult<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "unavailable"; reason: ReadFailure };
type EvidenceGroup = {
  key: string;
  document: DocumentTarget | null;
  content: ContentTarget | null;
  operations: ReviewEvidenceOperation[];
};

export type ReviewEvidenceContentState = {
  reviewId: string;
  status: ContentStatus;
};

type ReviewEvidencePanelProps = {
  workspaceId: string;
  review: ReviewItem;
  onContentStateChange?: (state: ReviewEvidenceContentState) => void;
  onRefreshReview?: () => void;
  showDiagnostics?: boolean;
};

/**
 * Read-only evidence. Document navigation and candidate reads use only the
 * actor-scoped document/content projection; raw operation IDs and diff refs
 * are diagnostics, never inputs to a guessed reader or version URL.
 */
export function ReviewEvidencePanel({
  workspaceId,
  review,
  onContentStateChange,
  onRefreshReview,
  showDiagnostics = false,
}: ReviewEvidencePanelProps) {
  const { t } = useTranslation("review");
  const groups = useMemo(
    () => groupEvidenceOperations(review),
    [review],
  );
  const [statuses, setStatuses] = useState<Record<string, ContentStatus>>({});
  const callbackRef = useRef(onContentStateChange);
  useEffect(() => {
    callbackRef.current = onContentStateChange;
  }, [onContentStateChange]);
  const onGroupStatusChange = useCallback((key: string, status: ContentStatus) => {
    setStatuses((current) =>
      current[key] === status ? current : { ...current, [key]: status },
    );
  }, []);
  const groupStatuses = groups.map((group) =>
    group.content?.mode === "metadata_only"
      ? "ready"
      : statuses[group.key] ?? (canReadExactContext(group) ? "loading" : "unavailable"),
  );
  const status: ContentStatus = groupStatuses.includes("unavailable") || groups.length === 0
    ? "unavailable"
    : groupStatuses.includes("loading")
      ? "loading"
      : "ready";
  useEffect(() => {
    callbackRef.current?.({ reviewId: review.id, status });
  }, [review.id, status]);

  return (
    <section className="review-evidence-panel" aria-label={t("document.targets", {
      defaultValue: "Review targets",
    })}>
      {groups.length === 0 ? (
        <StatusBanner tone="info">
          {t("content.noEvidence", {
            defaultValue: "No change evidence is available for this review.",
          })}
        </StatusBanner>
      ) : groups.map((group) => (
        <ReviewDocumentEvidence
          group={group}
          key={group.key}
          onRefreshReview={onRefreshReview}
          onStatusChange={onGroupStatusChange}
          workspaceId={workspaceId}
        />
      ))}
      {showDiagnostics && review.evidence ? <EvidenceDiagnostics review={review} /> : null}
    </section>
  );
}

function ReviewDocumentEvidence({
  group,
  workspaceId,
  onStatusChange,
  onRefreshReview,
}: {
  group: EvidenceGroup;
  workspaceId: string;
  onStatusChange: (key: string, status: ContentStatus) => void;
  onRefreshReview?: () => void;
}) {
  const { t } = useTranslation("review");
  const headingId = useId();
  const document = group.document;
  // A normalized redaction is authoritative, including a deliberately null title.
  const title = document
    ? document.title ?? t("document.unavailableTitle", { defaultValue: "Review target" })
    : group.operations[0]?.target_title
      ?? t("document.unavailableTitle", { defaultValue: "Review target" });
  const kindLabels = {
    context: t("document.kind.context", { defaultValue: "Context" }),
    library: t("document.kind.library", { defaultValue: "Library" }),
    agent: t("document.kind.agent", { defaultValue: "Agent" }),
  };
  const href = document?.can_open && document.document_id
    && safeReaderHref(document.reader_href)
    ? document.reader_href
    : null;
  const versionNo = group.content?.candidate_version_no;

  return (
    <section className="review-evidence-document" aria-labelledby={headingId}>
      <header className="review-document-heading">
        <FileText aria-hidden="true" size={18} />
        <div className="review-document-identity">
          <h3 id={headingId}>{title}</h3>
          <div className="review-document-meta">
            {document ? (
              <span className="review-document-kind">
                <span aria-hidden="true" className={`lc-dot-${document.document_kind}`} />
                {kindLabels[document.document_kind]}
              </span>
            ) : (
              <span>{t("document.kind.unknown", { defaultValue: "Target type unavailable" })}</span>
            )}
            {versionNo != null ? (
              <span>{t("content.reviewVersion", {
                version: versionNo,
                defaultValue: "Review version v{{version}}",
              })}</span>
            ) : null}
            {document?.slug ? <code>{document.slug}</code> : null}
          </div>
        </div>
        {href ? (
          <a
            aria-label={t("document.openCurrentAria", {
              title,
              defaultValue: "Open current document: {{title}} (new tab)",
            })}
            className="icon-button review-document-open"
            href={href}
            rel="noopener noreferrer"
            target="_blank"
          >
            {t("document.openCurrent", { defaultValue: "Open current document" })}
            <ExternalLink aria-hidden="true" size={15} />
          </a>
        ) : (
          <span className="review-document-unavailable">
            {t("document.openUnavailable", { defaultValue: "Current document unavailable" })}
          </span>
        )}
      </header>
      <ul className="review-evidence-operations">
        {group.operations.map((operation) => (
          <li key={operation.operation_id}>
            <strong>{domainLabel("review", "operation", operation.operation, t)}</strong>
            {operation.diff_summary ? <p>{operation.diff_summary}</p> : null}
          </li>
        ))}
      </ul>
      {group.content?.mode === "metadata_only" ? (
        <p className="review-content-notice">
          {t("content.metadataOperation", {
            defaultValue: "This review concerns the metadata changes described above.",
          })}
        </p>
      ) : canReadExactContext(group) ? (
        <ExactContextContent
          group={group}
          onStatusChange={onStatusChange}
          workspaceId={workspaceId}
        />
      ) : (
        <StatusBanner tone="info" action={onRefreshReview ? (
          <button className="icon-button" onClick={onRefreshReview} type="button">
            <RefreshCcw aria-hidden="true" size={14} />
            {t("content.refreshReview", { defaultValue: "Refresh review" })}
          </button>
        ) : undefined}>
          {group.content?.mode === "unsupported"
            ? t("content.unsupported", {
                defaultValue: "Review version content and comparison are not supported for this target.",
              })
            : t("content.candidateUnavailable", {
                defaultValue: "The reviewed version is unavailable. Check the review's current state and access.",
              })}
        </StatusBanner>
      )}
    </section>
  );
}

function ExactContextContent({
  group,
  workspaceId,
  onStatusChange,
}: {
  group: EvidenceGroup;
  workspaceId: string;
  onStatusChange: (key: string, status: ContentStatus) => void;
}) {
  const { t } = useTranslation("review");
  const tabsId = `review-content-${useId()}`;
  const content = group.content!;
  const documentId = group.document!.document_id!;
  const candidateId = content.candidate_version_id!;
  const baselineKind = content.baseline_kind;
  const baselineId = content.baseline_version_id ?? null;
  const canCompare = content.can_diff && (
    (baselineKind === "empty" && baselineId === null)
      || ((baselineKind === "published" || baselineKind === "parent")
        && Boolean(baselineId) && baselineId !== candidateId)
  );
  const [tab, setTab] = useState<ContentTab>(canCompare ? "comparison" : "original");
  const [reload, setReload] = useState(0);
  const requestKey = `${workspaceId}:${group.key}:${reload}`;
  const [result, setResult] = useState<{
    key: string;
    original: ReadResult<PagePayload>;
    comparison: ReadResult<PageVersionDiff>;
  } | null>(null);
  const current = result?.key === requestKey ? result : null;
  const original = current?.original ?? { status: "loading" as const };
  const comparison = current?.comparison ?? { status: "loading" as const };

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setResult({
      key: requestKey,
      original: { status: "loading" },
      comparison: canCompare
        ? { status: "loading" }
        : { status: "unavailable", reason: "unavailable" },
    });
    void readPage(workspaceId, documentId, candidateId).then((page) => {
      if (!active) return;
      const matches = page.workspace_id === workspaceId && page.id === documentId
        && page.version_id === candidateId && typeof page.body_md === "string";
      setResult((previous) => previous?.key === requestKey ? {
        ...previous,
        original: matches ? { status: "ready", data: page }
          : { status: "unavailable", reason: "mismatch" },
      } : previous);
    }).catch((error: unknown) => {
      if (!active) return;
      setResult((previous) => previous?.key === requestKey ? {
        ...previous,
        original: { status: "unavailable", reason: readFailure(error) },
      } : previous);
    });
    if (canCompare) {
      void diffPageVersions(
        workspaceId,
        documentId,
        baselineKind === "empty" ? null : baselineId,
        candidateId,
        { signal: controller.signal },
      ).then((diff) => {
        if (!active) return;
        const matches = diff.to_version_id === candidateId
          && (diff.from_version_id ?? null) === (baselineKind === "empty" ? null : baselineId)
          && diff.baseline === (baselineKind === "empty" ? "empty" : "version")
          && (content.candidate_version_no == null || diff.to_version_no === content.candidate_version_no)
          && (content.baseline_version_no == null || diff.from_version_no === content.baseline_version_no);
        setResult((previous) => previous?.key === requestKey ? {
          ...previous,
          comparison: matches ? { status: "ready", data: diff }
            : { status: "unavailable", reason: "mismatch" },
        } : previous);
      }).catch((error: unknown) => {
        if (!active) return;
        setResult((previous) => previous?.key === requestKey ? {
          ...previous,
          comparison: { status: "unavailable", reason: readFailure(error) },
        } : previous);
      });
    }
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    baselineId,
    baselineKind,
    canCompare,
    candidateId,
    content.candidate_version_no,
    content.baseline_version_no,
    documentId,
    requestKey,
    workspaceId,
  ]);
  useEffect(() => {
    onStatusChange(group.key, original.status);
  }, [group.key, onStatusChange, original.status]);
  const resolveContextMediaAsset = useMemo<ContextMediaAssetUrlResolver>(
    () => (assetId) => ({
      status: "ready",
      url: contextMediaPageVersionContentUrl(workspaceId, documentId, candidateId, assetId),
    }),
    [candidateId, documentId, workspaceId],
  );
  const version = content.candidate_version_no;
  const baselineLabel = baselineKind === "empty"
    ? t("content.emptyBaseline", { defaultValue: "Empty document" })
    : baselineKind === "published"
      ? t("content.publishedBaseline", {
          version: content.baseline_version_no ?? "?",
          defaultValue: "Published baseline v{{version}}",
        })
      : t("content.parentBaseline", {
          version: content.baseline_version_no ?? "?",
          defaultValue: "Parent version v{{version}}",
        });
  const candidateLabel = version == null
    ? t("content.reviewVersionUnknown", { defaultValue: "Review version" })
    : t("content.reviewVersion", { version, defaultValue: "Review version v{{version}}" });
  const retry = () => setReload((value) => value + 1);
  const hasBody = original.status === "ready"
    && Boolean(parsePageDocument(original.data.body_md).body.trim());
  const compareFailure = comparison.status === "unavailable" ? comparison.reason : null;
  const metadataLabels = {
    title: t("content.metadata.title", { defaultValue: "Title" }),
    tags: t("content.metadata.tags", { defaultValue: "Tags" }),
    aliases: t("content.metadata.aliases", { defaultValue: "Aliases" }),
    page_type: t("content.metadata.pageType", { defaultValue: "Page type" }),
    placement: t("content.metadata.placement", { defaultValue: "Placement" }),
  };

  return (
    <section className="review-version-content" aria-label={candidateLabel}>
      <div className="review-content-heading">
        <h4>{t("content.title", { defaultValue: "Change content" })}</h4>
        {canCompare ? <p>{baselineLabel} → {candidateLabel}</p> : <p>{candidateLabel}</p>}
      </div>
      {tab === "comparison" && original.status === "unavailable" ? (
        <StatusBanner tone="warning">
          {t("content.originalUnavailable", {
            defaultValue: "The exact review version could not be loaded. Refresh its content before approving.",
          })}
        </StatusBanner>
      ) : null}
      <ReaderTabs
        accent="review"
        active={tab}
        ariaLabel={t("content.tabsAria", { defaultValue: "Review version content" })}
        idBase={tabsId}
        onChange={setTab}
        tabs={[
          { key: "comparison", label: t("content.compare", { defaultValue: "Compare changes" }) },
          { key: "original", label: t("content.original", { defaultValue: "Review version content" }) },
        ]}
      />
      <div
        aria-labelledby={`${tabsId}-tab-${tab}`}
        className="review-content-tabpanel"
        id={`${tabsId}-panel-${tab}`}
        role="tabpanel"
        tabIndex={0}
      >
        {tab === "original" ? (
          original.status === "loading" ? <ContentLoading />
            : original.status === "unavailable" ? (
              <ContentError reason={original.reason} onRetry={retry} />
            ) : hasBody ? (
              <MarkdownView
                body={original.data.body_md}
                resolveContextMediaAsset={resolveContextMediaAsset}
                wikilinkTargets={(original.data.wikilink_targets ?? []).map((target) => ({ ...target, target_page_id: target.target_page_id ?? null, current_slug: target.current_slug ?? null }))}
                workspaceId={workspaceId}
              />
            ) : (
              <p className="review-content-notice">
                {t("content.emptyOriginal", { defaultValue: "This review version has no body content." })}
              </p>
            )
        ) : !canCompare ? (
          <div className="review-content-failure" role="status">
            <p>{content.unavailable_reason === "diff_too_large"
              ? t("content.tooLarge", { defaultValue: "These versions are too large to compare here." })
              : t("content.baselineUnavailable", {
                  defaultValue: "The comparison baseline is unavailable. Read the exact review version instead.",
                })}</p>
            <button className="icon-button" onClick={() => setTab("original")} type="button">
              {t("content.showOriginal", { defaultValue: "Read review version" })}
            </button>
          </div>
        ) : comparison.status === "loading" ? <ContentLoading />
          : comparison.status === "unavailable" ? (
            <div className="review-content-failure">
              <ContentError reason={compareFailure!} onRetry={retry} />
              <button className="icon-button" onClick={() => setTab("original")} type="button">
                {t("content.showOriginal", { defaultValue: "Read review version" })}
              </button>
            </div>
          ) : (
            <>
              {comparison.data.metadata_changes?.length ? (
                <p className="review-content-notice">
                  <strong>{t("content.metadataChanged", { defaultValue: "Changed metadata:" })}</strong>{" "}
                  {comparison.data.metadata_changes.map((field) => metadataLabels[field]).join(", ")}
                </p>
              ) : null}
              <DiffViewer
                ariaLabel={t("content.diffAria", {
                  from: baselineLabel,
                  to: candidateLabel,
                  defaultValue: "Changes from {{from}} to {{to}}",
                })}
                baseEndpointLabel={baselineLabel}
                comparedEndpointLabel={candidateLabel}
                defaultMode="unified"
                diff={comparison.data}
                showChangeMarkers
              />
            </>
          )}
      </div>
    </section>
  );
}

function ContentLoading() {
  const { t } = useTranslation("review");
  return (
    <p aria-busy="true" className="review-content-notice" role="status">
      {t("content.loading", { defaultValue: "Loading review version…" })}
    </p>
  );
}

function ContentError({ reason, onRetry }: { reason: ReadFailure; onRetry: () => void }) {
  const { t } = useTranslation("review");
  const message = reason === "too_large"
    ? t("content.tooLarge", { defaultValue: "These versions are too large to compare here." })
    : reason === "mismatch"
      ? t("content.versionMismatch", {
          defaultValue: "The response does not match this review version. Refresh the review before continuing.",
        })
      : reason === "unavailable"
        ? t("content.readUnavailable", {
            defaultValue: "This version or its access is no longer available.",
          })
        : t("content.loadFailed", { defaultValue: "Could not load this review version." });
  return (
    <StatusBanner tone="warning" action={reason !== "too_large" ? (
      <button className="icon-button" onClick={onRetry} type="button">
        <RefreshCcw aria-hidden="true" size={14} />
        {t("content.retry", { defaultValue: "Reload content" })}
      </button>
    ) : undefined}>
      {message}
    </StatusBanner>
  );
}

function EvidenceDiagnostics({ review }: { review: ReviewItem }) {
  const { t } = useTranslation("review");
  const evidence = review.evidence!;
  return (
    <details className="review-evidence-diagnostics">
      <summary>{t("content.diagnostics", { defaultValue: "Change evidence details" })}</summary>
      <dl className="detail-list">
        <div>
          <dt>{t("evidence.changeStatus", { defaultValue: "Change status" })}</dt>
          <dd>{domainLabel("review", "changeSetStatus", evidence.change_set_status, t)}</dd>
        </div>
        <div>
          <dt>{t("evidence.reason", { defaultValue: "Reason" })}</dt>
          <dd>{evidence.reason ?? t("evidence.none", { defaultValue: "none" })}</dd>
        </div>
        <div>
          <dt>{t("detail.trigger", { defaultValue: "Trigger" })}</dt>
          <dd>{domainLabel("review", "triggerType", evidence.trigger_type, t)}</dd>
        </div>
        <div>
          <dt>{t("content.citationSkipped", { defaultValue: "Citation call skipped" })}</dt>
          <dd>{evidence.cite_call_skipped
            ? t("content.yes", { defaultValue: "Yes" })
            : t("content.no", { defaultValue: "No" })}</dd>
        </div>
      </dl>
      <p className="review-content-notice">
        {t("content.citationNotValidation", {
          defaultValue: "This records whether citation calls were skipped; it is not a citation accuracy verdict.",
        })}
      </p>
      <p className="review-content-notice">
        {t("content.sectionUnavailable", {
          defaultValue: "The evidence does not provide a section location; document actions open the whole document.",
        })}
      </p>
      <ul className="review-operation-diagnostics">
        {evidence.operations.map((operation) => (
          <li key={operation.operation_id}>
            <strong>{domainLabel("review", "operation", operation.operation, t)}</strong>
            <dl className="detail-list">
              <div>
                <dt>{t("content.operationId", { defaultValue: "Operation ID" })}</dt>
                <dd><code>{operation.operation_id}</code></dd>
              </div>
              <div>
                <dt>{t("content.targetType", { defaultValue: "Target type" })}</dt>
                <dd><code>{operation.target_type}</code></dd>
              </div>
              {operation.diff_ref ? (
                <div>
                  <dt>{t("content.diffReference", { defaultValue: "Change reference" })}</dt>
                  <dd><code>{operation.diff_ref}</code></dd>
                </div>
              ) : null}
            </dl>
          </li>
        ))}
      </ul>
    </details>
  );
}

function canReadExactContext(group: EvidenceGroup) {
  return group.document?.document_kind === "context"
    && Boolean(group.document.document_id)
    && group.content?.mode === "context_version"
    && group.content.can_read
    && Boolean(group.content.candidate_version_id);
}

function groupEvidenceOperations(review: ReviewItem): EvidenceGroup[] {
  const groups = new Map<string, EvidenceGroup>();
  for (const operation of review.evidence?.operations ?? []) {
    const document = operation.document ?? null;
    const content = operation.content ?? null;
    const key = JSON.stringify([
      review.id,
      review.updated_at,
      document?.document_kind,
      document?.document_id ?? operation.operation_id,
      content?.mode,
      content?.candidate_version_id ?? (content?.mode === "metadata_only" ? null : operation.operation_id),
      content?.candidate_version_no,
      content?.baseline_kind,
      content?.baseline_version_id,
      content?.baseline_version_no,
      content?.can_read,
      content?.can_diff,
      content?.unavailable_reason,
    ]);
    const existing = groups.get(key);
    if (existing) {
      existing.operations.push(operation);
    } else {
      groups.set(key, { key, document, content, operations: [operation] });
    }
  }
  const result = [...groups.values()];
  const exactGroups = result.filter(canReadExactContext);
  const companions = new Set<EvidenceGroup>();
  const operationOrder = new Map(
    (review.evidence?.operations ?? []).map((operation, index) => [operation, index]),
  );
  for (const group of result) {
    // Only the actor-scoped server projection can associate a metadata change
    // with an exact Context candidate. Unknown targets and other versions stay
    // separate, and unsupported content continues to block readiness.
    if (group.content?.mode !== "metadata_only"
      || group.document?.document_kind !== "context"
      || !group.document.document_id || !group.content.candidate_version_id) continue;
    const matches = exactGroups.filter((exact) =>
      exact.document?.document_kind === group.document?.document_kind
      && exact.document?.document_id === group.document?.document_id
      && exact.content?.candidate_version_id === group.content?.candidate_version_id,
    );
    if (matches.length !== 1) continue;
    matches[0].operations.push(...group.operations);
    matches[0].operations.sort((left, right) =>
      (operationOrder.get(left) ?? 0) - (operationOrder.get(right) ?? 0),
    );
    companions.add(group);
  }
  return result.filter((group) => !companions.has(group));
}

function safeReaderHref(href: string | null | undefined): href is string {
  return Boolean(href && href.startsWith("/") && !href.startsWith("//")
    && !/[\u0000-\u0020\\]/.test(href));
}

function readFailure(error: unknown): ReadFailure {
  if (error instanceof ApiError) {
    if (error.body.error_code === "page_diff_too_large") return "too_large";
    if ([401, 403, 404, 410].includes(error.status)) return "unavailable";
  }
  return "failed";
}
