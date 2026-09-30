import { GitCompare } from "lucide-react";
import type { TFunction } from "i18next";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  McpChatEditSummary,
  McpChatSessionDiff,
  McpChatTimelineItem,
} from "../../shared/api/operations";
import { diffPageVersions, type PageVersionDiff } from "../../shared/api/pages";
import { DiffViewerDialog } from "../../shared/components/DiffViewerDialog";
import { useMediaQuery } from "../../shared/hooks/useMediaQuery";
import { similarityPercent } from "./chatPresentation";

type EditOperation = NonNullable<McpChatEditSummary["operations"]>[number];
type TextPreview = NonNullable<McpChatEditSummary["body_preview"]>;

// A patch call can carry dozens of operations. The card is one of many in a
// step, so it lists the first few and lets the reader ask for the rest.
const OPERATION_PREVIEW_LIMIT = 5;
const SECTION_SUMMARY_LIMIT = 3;
const NARROW_DIFF_QUERY = "(max-width: 640px)";

// Preview fields holding the text an operation replaced. Every other field
// (new_text, content, title) is what the operation wrote.
const BEFORE_FIELDS = new Set(["old_text"]);

/**
 * What a write call changed, on the call card itself: one summary line
 * (operations, sections, size), a short before/after preview per operation,
 * and the session's cumulative diff when the server offers one.
 *
 * Everything shown is the server's permission-gated projection
 * (`edit_summary`, `session_diff`); nothing is parsed out of `input_summary`.
 */
export function ChatEditCard({
  item,
  workspaceId,
}: {
  item: McpChatTimelineItem;
  workspaceId: string;
}) {
  const { t } = useTranslation("chats");
  const [showAll, setShowAll] = useState(false);
  const summary = item.edit_summary ?? null;
  const sessionDiff = item.session_diff ?? null;
  if (!summary && !sessionDiff) {
    return null;
  }
  const operations = summary?.operations ?? [];
  const visibleOperations = showAll
    ? operations
    : operations.slice(0, OPERATION_PREVIEW_LIMIT);
  const hiddenOperations = operations.length - visibleOperations.length;
  // Whole-body updates and page creation have no operations; their preview is
  // the first changed region, or the start of the new body.
  const firstChange = operations.length === 0 ? summary?.first_change ?? null : null;
  const bodyPreview = operations.length === 0 && !firstChange
    ? summary?.body_preview ?? null
    : null;
  const summaryLine = summary ? editSummaryLine(summary, t) : null;
  const note = summary?.previews_withheld
    ? t("detail.edit.previewsWithheld")
    : summary?.previews_purged
      ? t("detail.edit.previewsPurged")
      : null;

  return (
    <section
      aria-label={t("detail.edit.label")}
      className="mcp-chat-edit-card"
    >
      {summaryLine ? <p className="mcp-chat-edit-summary">{summaryLine}</p> : null}
      {visibleOperations.length > 0 ? (
        <ol className="mcp-chat-edit-ops">
          {visibleOperations.map((operation) => (
            <EditOperationRow key={operation.index} operation={operation} />
          ))}
        </ol>
      ) : null}
      {hiddenOperations > 0 ? (
        <button
          className="mcp-strip-toggle"
          type="button"
          onClick={() => setShowAll(true)}
        >
          {t("detail.edit.showAllOperations", { count: operations.length })}
        </button>
      ) : null}
      {firstChange ? (
        <div className="mcp-chat-edit-op">
          <p className="mcp-chat-edit-op-head">
            {t("detail.edit.firstChange", { line: firstChange.line })}
          </p>
          <EditPreviewPair
            after={firstChange.after_preview ?? null}
            before={firstChange.before_preview ?? null}
          />
        </div>
      ) : null}
      {bodyPreview?.text ? (
        <div className="mcp-chat-edit-op">
          <p className="mcp-chat-edit-op-head">{t("detail.edit.bodyPreview")}</p>
          <div className="mcp-chat-edit-preview">
            <PreviewLine preview={bodyPreview} side="after" />
          </div>
        </div>
      ) : null}
      {summary && summary.preview_budget_exhausted > 0 ? (
        <p className="mcp-chat-edit-note">
          {t("detail.edit.budgetExhausted", {
            count: summary.preview_budget_exhausted,
          })}
        </p>
      ) : null}
      {note ? <p className="mcp-chat-edit-note">{note}</p> : null}
      {sessionDiff ? (
        <SessionDiffAction sessionDiff={sessionDiff} workspaceId={workspaceId} />
      ) : null}
    </section>
  );
}

function EditOperationRow({ operation }: { operation: EditOperation }) {
  const { t } = useTranslation("chats");
  const previews = (operation.previews ?? []).filter(
    (preview): preview is TextPreview & { text: string } => Boolean(preview.text),
  );
  const before = previews.filter((preview) => BEFORE_FIELDS.has(preview.field));
  const after = previews.filter((preview) => !BEFORE_FIELDS.has(preview.field));
  const facts = [
    operation.heading_path ? sectionLabel(operation.heading_path, t) : null,
    lineRangeLabel(operation.line_range ?? null, t),
    charDeltaLabel(operation.chars_added ?? null, operation.chars_removed ?? null, t),
  ].filter((fact): fact is string => Boolean(fact));
  return (
    <li className="mcp-chat-edit-op">
      <p className="mcp-chat-edit-op-head">
        <code>{operation.op}</code>
        {facts.map((fact) => (
          <span key={fact}>{fact}</span>
        ))}
        <MatchBadge operation={operation} />
      </p>
      {previews.length > 0 ? (
        <div className="mcp-chat-edit-preview">
          {before.map((preview) => (
            <PreviewLine key={preview.field} preview={preview} side="before" />
          ))}
          {after.map((preview) => (
            <PreviewLine key={preview.field} preview={preview} side="after" />
          ))}
        </div>
      ) : null}
    </li>
  );
}

// replace_text that did not match byte-for-byte says so on its row: a
// forgiven copy slip is a judgement the reader should be able to check.
function MatchBadge({ operation }: { operation: EditOperation }) {
  const { t } = useTranslation("chats");
  if (operation.match_kind === "fuzzy") {
    const percent =
      typeof operation.match_similarity === "number"
        ? similarityPercent(operation.match_similarity)
        : null;
    const hint = t("detail.edit.matchFuzzyHint");
    return (
      <span className="mcp-chat-edit-match is-fuzzy" title={hint}>
        {percent === null
          ? t("detail.edit.matchFuzzy", { percent: "?" })
          : t("detail.edit.matchFuzzy", { percent })}
        <span className="visually-hidden"> {hint}</span>
      </span>
    );
  }
  if (operation.match_kind === "normalized") {
    const hint = t("detail.edit.matchNormalizedHint");
    return (
      <span className="mcp-chat-edit-match" title={hint}>
        {t("detail.edit.matchNormalized")}
        <span className="visually-hidden"> {hint}</span>
      </span>
    );
  }
  return null;
}

function EditPreviewPair({
  after,
  before,
}: {
  after: string | null;
  before: string | null;
}) {
  if (!before && !after) {
    return null;
  }
  return (
    <div className="mcp-chat-edit-preview">
      {before ? (
        <PreviewLine preview={{ field: "before", text: before, truncated: false }} side="before" />
      ) : null}
      {after ? (
        <PreviewLine preview={{ field: "after", text: after, truncated: false }} side="after" />
      ) : null}
    </div>
  );
}

function PreviewLine({
  preview,
  side,
}: {
  preview: TextPreview;
  side: "before" | "after";
}) {
  const { t } = useTranslation("chats");
  return (
    <p className={`mcp-chat-edit-line is-${side}`}>
      <span aria-hidden="true" className="mcp-chat-edit-marker">
        {side === "before" ? "−" : "+"}
      </span>
      {/* Trailing space so a screen reader does not run the label into the
          text ("Before:Run..."). */}
      <span className="visually-hidden">
        {`${side === "before" ? t("detail.edit.before") : t("detail.edit.after")} `}
      </span>
      <span className="mcp-chat-edit-text">
        {preview.text}
        {preview.truncated ? "…" : ""}
      </span>
    </p>
  );
}

function SessionDiffAction({
  sessionDiff,
  workspaceId,
}: {
  sessionDiff: McpChatSessionDiff;
  workspaceId: string;
}) {
  const { t } = useTranslation("chats");
  const narrow = useMediaQuery(NARROW_DIFF_QUERY);
  const [open, setOpen] = useState(false);
  const [diff, setDiff] = useState<PageVersionDiff | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { page_id: pageId, from_version_id: fromId, to_version_id: toId } = sessionDiff;

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    // Computed on request, never stored: the diff is always between the two
    // versions as they are now, under the reader's own permissions.
    const controller = new AbortController();
    setDiff(null);
    setError(null);
    setLoading(true);
    diffPageVersions(workspaceId, pageId, fromId, toId, { signal: controller.signal })
      .then((next) => {
        if (!controller.signal.aborted) {
          setDiff(next);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setError(t("detail.edit.sessionDiffFailed"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [open, workspaceId, pageId, fromId, toId, t]);

  return (
    <>
      <button
        className="icon-button mcp-chat-edit-diff"
        type="button"
        onClick={() => setOpen(true)}
      >
        <GitCompare size={15} aria-hidden="true" />
        <span>{t("detail.edit.sessionDiff")}</span>
      </button>
      {open ? (
        <DiffViewerDialog
          baseEndpointLabel={
            fromId === null
              ? t("detail.edit.sessionDiffEmptyBase")
              : t("detail.edit.sessionDiffBase")
          }
          comparedEndpointLabel={t("detail.edit.sessionDiffCompared")}
          defaultMode={narrow ? "unified" : "split"}
          diff={diff}
          error={error}
          fullScreen={narrow}
          loading={loading}
          onClose={() => setOpen(false)}
          showChangeMarkers
          title={t("detail.edit.sessionDiffTitle")}
        />
      ) : null}
    </>
  );
}

export function editSummaryLine(
  summary: McpChatEditSummary,
  t: TFunction<"chats">,
): string | null {
  const parts: string[] = [];
  const counts = Object.entries(summary.operation_counts ?? {});
  if (counts.length > 0) {
    parts.push(counts.map(([op, count]) => (count > 1 ? `${op} ×${count}` : op)).join(", "));
  }
  const sections = summary.changed_sections ?? [];
  if (sections.length > 0) {
    const shown = sections
      .slice(0, SECTION_SUMMARY_LIMIT)
      .map((path) => sectionLabel(path, t));
    const more = sections.length - shown.length;
    parts.push(
      more > 0
        ? t("detail.edit.moreSections", { sections: shown.join(", "), count: more })
        : shown.join(", "),
    );
  }
  const delta = charDeltaLabel(summary.chars_added ?? null, summary.chars_removed ?? null, t);
  if (delta) {
    // An `input` summary is sized from what the call sent, not measured on
    // the document, so it is marked as the estimate it is.
    parts.push(
      summary.source === "input"
        ? t("detail.edit.estimated", { value: delta })
        : delta,
    );
  }
  return parts.length > 0 ? parts.join(" · ") : null;
}

function sectionLabel(path: string[], t: TFunction<"chats">): string {
  return path.length > 0 ? `§ ${path.join(" › ")}` : t("detail.edit.documentStart");
}

function lineRangeLabel(range: number[] | null, t: TFunction<"chats">): string | null {
  if (!range || range.length === 0) {
    return null;
  }
  const [start, end = start] = range;
  return start === end
    ? t("detail.edit.line", { line: start })
    : t("detail.edit.lines", { start, end });
}

function charDeltaLabel(
  added: number | null,
  removed: number | null,
  t: TFunction<"chats">,
): string | null {
  if (added !== null && removed !== null) {
    return t("detail.edit.chars", {
      added: added.toLocaleString(),
      removed: removed.toLocaleString(),
    });
  }
  if (added !== null) {
    return t("detail.edit.charsAdded", { added: added.toLocaleString() });
  }
  if (removed !== null) {
    return t("detail.edit.charsRemoved", { removed: removed.toLocaleString() });
  }
  return null;
}

/**
 * Whether any call still holds preview text an owner could delete. A withheld
 * summary counts: its text exists, this reader is just not shown it.
 */
export function chatHasEditPreviews(timeline: McpChatTimelineItem[]): boolean {
  return timeline.some((item) => {
    const summary = item.edit_summary;
    if (!summary || summary.previews_purged) {
      return false;
    }
    return Boolean(
      summary.previews_withheld
      || summary.body_preview?.text
      || summary.first_change?.before_preview
      || summary.first_change?.after_preview
      || (summary.operations ?? []).some((operation) =>
        (operation.previews ?? []).some((preview) => Boolean(preview.text))),
    );
  });
}
