import {
  AlertTriangle,
  Archive,
  ArchiveRestore,
  Book,
  Bot,
  Check,
  ChevronLeft,
  ExternalLink,
  FileText,
  Image as ImageIcon,
  Info,
  Pencil,
  Save,
  Search,
  Trash2,
  X,
} from "lucide-react";
import type { TFunction } from "i18next";
import type { KeyboardEvent, ReactNode } from "react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  archiveMcpChat,
  purgeMcpChatPreviews,
  readMcpChat,
  unarchiveMcpChat,
  updateMcpChatTitle,
  type McpChatDetail,
  type McpChatTimelineItem,
} from "../../shared/api/operations";
import {
  currentWorkspaceSelection,
  getCurrentWorkspaceId,
  type WorkspaceSummary,
} from "../../shared/api/workspace";
import { AdvancedSection } from "../../shared/components/AdvancedSection";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { Dialog, DialogCloseButton } from "../../shared/components/Dialog";
import { ReaderTabs } from "../../shared/components/ReaderTabs";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { useMediaQuery } from "../../shared/hooks/useMediaQuery";
import {
  formatDurationMs,
  formatLatencyMs,
  formatVersionTimestamp,
} from "../../shared/lib/datetime";
import {
  chatFlowPhaseForAction,
  chatFlowRepresentativeItem,
  distinctSubjects,
  groupChatFlow,
  isChatFlowError,
  totalLatencyMs,
  type ChatFlowPhase,
  type ChatFlowStep,
} from "./chatFlow";
import { artifactVerbLabel, chatSummaryLine } from "./chatLabels";
import { safeChatErrorMessage } from "./chatErrors";
import { ChatEditCard, chatHasEditPreviews } from "./ChatEditCard";
import {
  ChatIdentifier,
  chatInputLabel,
  ChatWorkspaces,
  isIdentifierField,
  isResourceTypeField,
  schemaVersion,
  TypeChip,
  WorkspaceInput,
} from "./ChatInputPresentation";
import {
  chatPresentationLine,
  humanizeIdentifier,
  similarityPercent,
  toolTitleLabel,
} from "./chatPresentation";
import { ClientChip, ClientIdentity } from "./ClientChip";
import { ToolInputValue, isLongToolValue } from "./ToolInputValue";

const NARROW_FLOW_QUERY = "(max-width: 1024px)";

export function ChatsDetailScreen() {
  const { t } = useTranslation("chats");
  const { chatId = "" } = useParams();
  const navigate = useNavigate();
  const [editTitle, setEditTitle] = useState<string | null>(null);
  const [savingTitle, setSavingTitle] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [unarchiving, setUnarchiving] = useState(false);
  const mutationActionRef = useRef<HTMLButtonElement | null>(null);
  const titleHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const titleInputRef = useRef<HTMLInputElement | null>(null);
  const {
    data: state,
    error,
    reload,
    setData,
  } = useAsyncLoad<{
    workspaceId: string;
    chat: McpChatDetail;
    isOwner: boolean;
    workspaces: WorkspaceSummary[];
  }>(
    async () => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId || !chatId) {
        throw new Error(t("detail.errors.notFound"));
      }
      const chat = await readMcpChat(workspaceId, chatId);
      // Only gates whether the owner-only action is offered; the server
      // refuses anyone else regardless.
      const isOwner = currentWorkspaceSelection().workspace?.role === "owner";
      return {
        workspaceId, chat, isOwner,
        workspaces: currentWorkspaceSelection().data.workspaces,
      };
    },
    [chatId],
    {
      errorMessage: t("detail.errors.loadFailed"),
      formatError: (caught, fallback) =>
        safeChatErrorMessage(caught, fallback, [t("detail.errors.notFound")]),
    },
  );

  async function saveTitle() {
    if (!state || editTitle === null || savingTitle) {
      return;
    }
    const trimmed = editTitle.trim();
    if (!trimmed) {
      // Don't PATCH a blank/whitespace title: it would surface a raw 422 and,
      // before the backend guard, could persist an empty title that locks out
      // auto-titling. Keep the editor open with guidance (C14, pairs with S3).
      setActionError(t("detail.errors.titleEmpty"));
      return;
    }
    setActionError(null);
    setActionNotice(null);
    setSavingTitle(true);
    try {
      const updatedChat = await updateMcpChatTitle(
        state.workspaceId,
        state.chat.id,
        trimmed,
      );
      setData((current) => current
        ? { ...current, chat: updatedChat }
        : current);
      setEditTitle(null);
      setActionNotice(t("detail.titleSaveSuccess"));
      window.requestAnimationFrame(() => titleHeadingRef.current?.focus());
    } catch {
      setActionError(t("detail.errors.titleUpdateFailed"));
      window.requestAnimationFrame(() => titleInputRef.current?.focus());
    } finally {
      setSavingTitle(false);
    }
  }

  // A6: restoring is non-destructive, so unarchive in place and reload the
  // detail (no confirm dialog) rather than bouncing to the list.
  async function unarchive() {
    if (!state || unarchiving) {
      return;
    }
    setUnarchiving(true);
    setActionError(null);
    setActionNotice(null);
    try {
      await unarchiveMcpChat(state.workspaceId, state.chat.id);
      setData((current) => current
        ? {
            ...current,
            chat: { ...current.chat, archived_at: null },
          }
        : current);
      setActionNotice(t("detail.restoreSuccess"));
      reload();
    } catch {
      setActionError(t("detail.errors.unarchiveFailed"));
    } finally {
      setUnarchiving(false);
      window.requestAnimationFrame(() => mutationActionRef.current?.focus());
    }
  }

  if (!state) {
    return (
      <div className="workspace-screen single-column">
        <section className="feature-main">
          <Breadcrumbs
            items={[{ label: t("breadcrumb"), href: "/chats" }, { label: t("detail.breadcrumb") }]}
          />
          {error ? (
            // A terminally failed load (404 chat, non-editor 403) must not
            // keep the spinner running; offer a retry and a way back instead.
            <>
              <StatusBanner
                action={
                  <button
                    className="icon-button text-button"
                    type="button"
                    onClick={reload}
                  >
                    {t("detail.retry")}
                  </button>
                }
                tone="warning"
              >
                {error}
              </StatusBanner>
              <div>
                <Link className="icon-button text-button" to="/chats">
                  <ChevronLeft size={16} aria-hidden="true" />
                  {t("detail.backToChats")}
                </Link>
              </div>
            </>
          ) : (
            <RouteLoading label={t("detail.loading")} />
          )}
        </section>
      </div>
    );
  }

  const { chat } = state;
  // The stored title remains editable/debuggable, but the visible title is
  // composed from semantic read-time facts so action/kind/tool labels localize.
  const displayTitle = chatPresentationLine(chat, t);
  const visibleTitle = editTitle ?? displayTitle;
  const summaryLine = chatSummaryLine(chat);
  const canPurgePreviews = state.isOwner && chatHasEditPreviews(chat.timeline);

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main mcp-chat-detail-screen">
        <Breadcrumbs
          items={[{ label: t("breadcrumb"), href: "/chats" }, { label: displayTitle }]}
        />
        <div className="feature-header mcp-chat-detail-header">
          <div className="mcp-chat-title-bar">
            <div className="mcp-chat-title-main">
              <ClientChip client={chat.client} />
              <div className="mcp-chat-title-block">
                {editTitle === null ? (
                  <div className="mcp-chat-title-line">
                    <h1 ref={titleHeadingRef} tabIndex={-1}>{displayTitle}</h1>
                    {/* State belongs to the object, not to the button group it
                        used to sit inside. */}
                    {chat.archived_at ? (
                      <StatusPill
                        status="archived"
                        label={t("card.archived")}
                        title={t("card.archived")}
                        compact
                      />
                    ) : null}
                  </div>
                ) : (
                  <div className="mcp-title-edit-row">
                    <input
                      ref={titleInputRef}
                      aria-label={t("detail.titleLabel")}
                      disabled={savingTitle}
                      value={visibleTitle}
                      onChange={(event) => setEditTitle(event.currentTarget.value)}
                    />
                    <button
                      aria-label={savingTitle
                        ? t("detail.savingTitle")
                        : t("detail.saveTitle")}
                      className="icon-button"
                      disabled={savingTitle || (editTitle ?? "").trim().length === 0}
                      type="button"
                      onClick={saveTitle}
                    >
                      <Save size={15} aria-hidden="true" />
                      <span>
                        {savingTitle ? t("detail.savingTitle") : t("detail.saveTitle")}
                      </span>
                    </button>
                    <button
                      aria-label={t("detail.cancelTitleEdit")}
                      className="icon-button"
                      disabled={savingTitle}
                      type="button"
                      onClick={() => setEditTitle(null)}
                    >
                      <X size={15} aria-hidden="true" />
                    </button>
                  </div>
                )}
              </div>
            </div>
            {editTitle === null ? (
              // One cluster, shared with the other detail screens, instead of
              // a bespoke two-row header that scattered back/archive/edit
              // across opposite screen edges.
              <div className="detail-heading-actions">
                <button
                  className="icon-button"
                  type="button"
                  onClick={() => {
                    setActionNotice(null);
                    setEditTitle(displayTitle);
                  }}
                >
                  <Pencil size={15} aria-hidden="true" />
                  <span>{t("detail.editTitle")}</span>
                </button>
                {chat.archived_at ? (
                  <button
                    ref={mutationActionRef}
                    className="icon-button"
                    type="button"
                    disabled={unarchiving}
                    onClick={unarchive}
                  >
                    <ArchiveRestore size={16} aria-hidden="true" />
                    {unarchiving ? t("detail.restoring") : t("detail.restore")}
                  </button>
                ) : (
                  <button
                    ref={mutationActionRef}
                    className="icon-button"
                    type="button"
                    onClick={() => setArchiveOpen(true)}
                  >
                    <Archive size={16} aria-hidden="true" />
                    {t("detail.archive")}
                  </button>
                )}
                {canPurgePreviews ? (
                  <button
                    className="icon-button"
                    type="button"
                    onClick={() => setPurgeOpen(true)}
                  >
                    <Trash2 size={15} aria-hidden="true" />
                    <span>{t("detail.purgePreviews.open")}</span>
                  </button>
                ) : null}
              </div>
            ) : null}
            {/* Last child on purpose: the flex row wraps here, so the copy is a
                full-width second line under the title and its actions instead
                of a 12px sentence floating beside a 22px h1. */}
            <div className="mcp-chat-title-copy">
              {summaryLine ? (
                <p className="mcp-chat-detail-summary">
                  {summaryLine.source ? (
                    <span className="mcp-chat-intent-source">
                      {t(`intentSource.${summaryLine.source}`)}
                    </span>
                  ) : null}
                  {summaryLine.text}
                </p>
              ) : null}
            </div>
          </div>
        </div>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        {actionError ? <StatusBanner tone="warning">{actionError}</StatusBanner> : null}
        {actionNotice ? <StatusBanner tone="info">{actionNotice}</StatusBanner> : null}
        <section
          aria-atomic="true"
          aria-labelledby="mcp-chat-record-notice-title"
          className="mcp-chat-record-region status-banner status-banner-info"
          role="note"
        >
          {/* This is persistent explanatory copy, not a mutation update. Keep
              the shared info-banner appearance without StatusBanner's live
              role="status", so merely opening the route does not announce it. */}
          <Info aria-hidden="true" size={16} />
          <div className="mcp-chat-record-notice">
            <strong id="mcp-chat-record-notice-title">
              {t("detail.recordNoticeTitle")}
            </strong>
            <span>{t("detail.recordNoticeBody")}</span>
          </div>
        </section>
        <SessionInfoSection chat={chat} />
        {chat.linked_artifacts.length > 0 ? <ArtifactsSection chat={chat} /> : null}
        <ChatWorkspaces.Provider value={state.workspaces}>
          <ChatFlowSection timeline={chat.timeline} workspaceId={state.workspaceId} />
        </ChatWorkspaces.Provider>
      </section>
      {archiveOpen ? (
        <ArchiveChatDialog
          chatTitle={displayTitle}
          onClose={() => setArchiveOpen(false)}
          onSubmit={async () => {
            await archiveMcpChat(state.workspaceId, chat.id);
            navigate("/chats", {
              replace: true,
              state: {
                chatArchiveSuccess: {
                  chatId: chat.id,
                },
              },
            });
          }}
        />
      ) : null}
      {purgeOpen ? (
        <PurgePreviewsDialog
          chatTitle={displayTitle}
          onClose={() => setPurgeOpen(false)}
          onSubmit={async () => {
            const result = await purgeMcpChatPreviews(state.workspaceId, chat.id);
            setPurgeOpen(false);
            setActionError(null);
            setActionNotice(
              t("detail.purgePreviews.success", { count: result.purged_call_count }),
            );
            // The action goes away with the previews, so focus lands on the
            // heading rather than on a button that no longer exists.
            window.requestAnimationFrame(() => titleHeadingRef.current?.focus());
            reload();
          }}
        />
      ) : null}
    </div>
  );
}

function PurgePreviewsDialog({
  chatTitle,
  onClose,
  onSubmit,
}: {
  chatTitle: string;
  onClose: () => void;
  onSubmit: () => Promise<void>;
}) {
  const { t } = useTranslation("chats");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (error) {
      submitRef.current?.focus();
    }
  }, [error]);

  async function submit() {
    if (submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit();
    } catch {
      setError(t("detail.purgePreviews.error"));
      setSubmitting(false);
    }
  }

  function requestClose() {
    if (!submitting) {
      onClose();
    }
  }

  return (
    <Dialog
      dismissible={!submitting}
      labelledBy="purge-chat-previews-title"
      onClose={requestClose}
    >
      <div className="dialog-form">
        <header className="dialog-header">
          <div>
            <p className="eyebrow">{t("breadcrumb")}</p>
            <h2 id="purge-chat-previews-title">
              {t("detail.purgePreviews.title", { title: chatTitle })}
            </h2>
            <p className="feature-subtitle">{t("detail.purgePreviews.body")}</p>
          </div>
          <DialogCloseButton
            disabled={submitting}
            label={t("detail.purgePreviews.close")}
            onClick={requestClose}
          />
        </header>
        {submitting ? (
          <StatusBanner tone="info">{t("detail.purgePreviews.pending")}</StatusBanner>
        ) : null}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="dialog-actions">
          <button
            className="icon-button text-button"
            disabled={submitting}
            type="button"
            onClick={requestClose}
          >
            {t("detail.purgePreviews.cancel")}
          </button>
          <button
            ref={submitRef}
            className="icon-button danger"
            disabled={submitting}
            type="button"
            onClick={submit}
          >
            {submitting
              ? t("detail.purgePreviews.submitting")
              : t("detail.purgePreviews.action")}
          </button>
        </div>
      </div>
    </Dialog>
  );
}

function ArchiveChatDialog({
  chatTitle,
  onClose,
  onSubmit,
}: {
  chatTitle: string;
  onClose: () => void;
  onSubmit: () => Promise<void>;
}) {
  const { t } = useTranslation("chats");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (error) {
      submitRef.current?.focus();
    }
  }, [error]);

  async function submit() {
    if (submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit();
    } catch {
      setError(t("archiveConfirm.error"));
      setSubmitting(false);
    }
  }

  function requestClose() {
    if (!submitting) {
      onClose();
    }
  }

  return (
    <Dialog
      dismissible={!submitting}
      labelledBy="archive-chat-title"
      onClose={requestClose}
    >
      <div className="dialog-form">
        <header className="dialog-header">
          <div>
            <p className="eyebrow">{t("breadcrumb")}</p>
            <h2 id="archive-chat-title">
              {t("archiveConfirm.title", {
                defaultValue: "Archive {{title}}?",
                title: chatTitle,
              })}
            </h2>
            <p className="feature-subtitle">
              {t("archiveConfirm.body", {
                defaultValue:
                  "Archiving hides the chat from the default list. Restore it any time from the Archived filter.",
              })}
            </p>
          </div>
          <DialogCloseButton
            disabled={submitting}
            label={t("archiveConfirm.close", {
              defaultValue: "Close archive dialog",
            })}
            onClick={requestClose}
          />
        </header>
        {submitting ? (
          <StatusBanner tone="info">{t("archiveConfirm.pending")}</StatusBanner>
        ) : null}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="dialog-actions">
          <button
            className="icon-button text-button"
            disabled={submitting}
            type="button"
            onClick={requestClose}
          >
            {t("archiveConfirm.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            ref={submitRef}
            className="icon-button primary"
            disabled={submitting}
            type="button"
            onClick={submit}
          >
            {submitting
              ? t("archiveConfirm.submitting", { defaultValue: "Archiving" })
              : t("archiveConfirm.action", { defaultValue: "Archive" })}
          </button>
        </div>
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Session info: everything that used to be a permanent 7-tile strip is now a
// disclosure, collapsed by default so a returning reader lands on the flow
// (the part that actually says what the agent did) instead of a metadata
// wall. The one-time collapse choice is remembered across chats.
// ---------------------------------------------------------------------------

const SESSION_INFO_OPEN_KEY = "dtxwiki.chatsDetailSessionInfoOpen";
const ARTIFACTS_OPEN_KEY = "dtxwiki.chatsDetailArtifactsOpen";

function readStoredOpen(key: string, fallback: boolean): boolean {
  if (typeof window === "undefined") {
    return fallback;
  }
  const value = window.localStorage.getItem(key);
  return value === null ? fallback : value === "true";
}

function writeStoredOpen(key: string, value: boolean) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(key, String(value));
}

function usePersistedOpen(key: string, fallback: boolean) {
  const [open, setOpen] = useState(() => readStoredOpen(key, fallback));
  return [open, (next: boolean) => {
    setOpen(next);
    writeStoredOpen(key, next);
  }] as const;
}

function SessionInfoSection({ chat }: { chat: McpChatDetail }) {
  const { t } = useTranslation("chats");
  const [open, setOpen] = usePersistedOpen(SESSION_INFO_OPEN_KEY, false);
  const durationMs = durationBetween(chat.started_at, chat.last_activity_at);
  const { bootstrap } = useMemo(() => groupChatFlow(chat.timeline), [chat.timeline]);

  return (
    <AdvancedSection
      icon={null}
      label={t("detail.flow.sessionInfoLabel")}
      subtitle={t("detail.flow.sessionInfoSubtitle", {
        started: formatVersionTimestamp(chat.started_at),
        duration: formatDurationMs(durationMs, t),
      })}
      open={open}
      onOpenChange={setOpen}
    >
      <div className="mcp-chat-meta-strip">
        <div className="mcp-chat-session-facts">
          <MetaItem
            className="mcp-chat-meta-item--time"
            label={t("detail.meta.started")}
            value={formatVersionTimestamp(chat.started_at)}
          />
          <MetaItem
            className="mcp-chat-meta-item--time"
            label={t("detail.meta.lastActivity")}
            value={formatVersionTimestamp(chat.last_activity_at)}
          />
          <MetaItem
            label={t("units.duration")}
            value={formatDurationMs(durationMs, t)}
          />
          <MetaItem
            label={t("detail.meta.calls")}
            value={String(chat.tool_call_count)}
          />
          <MetaItem
            label={t("detail.meta.writes")}
            value={String(chat.write_count)}
          />
          <MetaItem
            label={t("detail.meta.errors")}
            value={String(chat.error_count)}
          />
        </div>
        <MetaItem
          className="mcp-chat-meta-item--client"
          label={t("detail.meta.client")}
          value={<ClientIdentity client={chat.client} />}
        />
      </div>
      {bootstrap.length > 0 ? <BootstrapNote items={bootstrap} /> : null}
    </AdvancedSection>
  );
}

function BootstrapNote({ items }: { items: McpChatTimelineItem[] }) {
  const { t } = useTranslation("chats");
  const distinctToolNames = Array.from(new Set(items.map((item) => item.tool_name)));
  const toolLabels = distinctToolNames.map((toolName) => toolTitleLabel(toolName, t));
  const latency = totalLatencyMs(items);
  return (
    <p className="mcp-chat-flow-bootstrap-note">
      <Info size={14} aria-hidden="true" />
      <span>
        {toolLabels.join(" · ")}
        {" — "}
        {t(
          items.length === 1
            ? "detail.flow.bootstrapCount_one"
            : "detail.flow.bootstrapCount_other",
          { count: items.length },
        )}
        {latency !== null ? ` · ${formatLatencyMs(latency)}` : ""}
      </span>
    </p>
  );
}

function MetaItem({ className, label, value }: { className?: string; label: string; value: ReactNode }) {
  return (
    <div className={`mcp-chat-meta-item${className ? ` ${className}` : ""}`}>
      <span>{label}</span>
      <div className="mcp-chat-meta-value">{value}</div>
    </div>
  );
}

const ARTIFACT_PREVIEW_LIMIT = 8;

function ArtifactsSection({ chat }: { chat: McpChatDetail }) {
  const { t } = useTranslation("chats");
  const [open, setOpen] = usePersistedOpen(ARTIFACTS_OPEN_KEY, false);
  const [showAll, setShowAll] = useState(false);
  const all = chat.linked_artifacts;
  const visible = showAll ? all : all.slice(0, ARTIFACT_PREVIEW_LIMIT);
  const hidden = all.length - visible.length;

  const verbCounts = new Map<string, number>();
  for (const artifact of all) {
    verbCounts.set(artifact.verb, (verbCounts.get(artifact.verb) ?? 0) + 1);
  }
  const subtitle = Array.from(verbCounts.entries())
    .map(([verb, count]) => `${artifactVerbLabel(verb, t)} ${count}`)
    .join(" · ");

  return (
    <AdvancedSection
      icon={null}
      label={t("detail.linkedArtifacts", { defaultValue: "Linked artifacts" })}
      subtitle={subtitle}
      trailing={
        <CountChip
          value={all.length}
          label={t(all.length === 1 ? "counts.artifact_one" : "counts.artifact_other")}
          compact
        />
      }
      open={open}
      onOpenChange={setOpen}
    >
      <div className="mcp-artifact-strip">
        {visible.map((artifact) => (
          <Link
            className="mcp-artifact-chip"
            data-kind={artifact.resource_kind}
            key={artifact.id}
            to={artifactNavigationPath(artifact)}
          >
            <span className="mcp-artifact-chip-icon" aria-hidden="true">
              {iconForArtifactKind(artifact.resource_kind)}
            </span>
            <span className="mcp-artifact-chip-body">
              <strong>
                {artifact.artifact_title
                  ?? t(`labels.artifactKind.${artifact.resource_kind}`, {
                    defaultValue: t("labels.artifactKind.unknown"),
                  })}
              </strong>
              <span className="mcp-artifact-chip-meta">
                <StatusPill
                  status={artifact.verb}
                  label={artifactVerbLabel(artifact.verb, t)}
                  title={artifactVerbLabel(artifact.verb, t)}
                  compact
                />
                <small>
                  {artifact.occurrence_count > 1
                    ? `${t("detail.artifactLinks", { count: artifact.occurrence_count })} · `
                    : ""}
                  {formatVersionTimestamp(artifact.last_occurred_at)}
                </small>
              </span>
            </span>
          </Link>
        ))}
      </div>
      {hidden > 0 ? (
        <button
          type="button"
          className="mcp-strip-toggle"
          onClick={() => setShowAll(true)}
        >
          {t("detail.showAllArtifacts", { count: all.length })}
        </button>
      ) : null}
    </AdvancedSection>
  );
}

// ---------------------------------------------------------------------------
// Flow: the timeline regrouped into phase steps (chatFlow.ts), shown as a
// compact rail beside a pinned detail panel. Both panes are sized to the
// viewport and scroll independently, so picking a step deep in a long rail
// never renders its detail above the reader's current scroll position.
// ---------------------------------------------------------------------------

function ChatFlowSection({
  timeline,
  workspaceId,
}: {
  timeline: McpChatTimelineItem[];
  workspaceId: string;
}) {
  const { t } = useTranslation("chats");
  const { steps } = useMemo(() => groupChatFlow(timeline), [timeline]);
  const narrow = useMediaQuery(NARROW_FLOW_QUERY);
  const [activeIndex, setActiveIndex] = useState(0);
  const nodeRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const railBodyRef = useRef<HTMLDivElement | null>(null);
  // Measured, not derived from the index. The spine's track is not the span
  // between the first and last dot: CSS insets it a fixed 22px from the rail
  // body, which is as tall as the scroll area rather than as tall as the
  // steps. A three-step chat gives a ~769px track holding its dots across the
  // top ~145px, so any index-to-percent formula lands the fill in the empty
  // space below the last card -- measured at 297px past dot 2 for a half
  // filled track. Row heights are not uniform either (a headline wraps, the
  // narrow layout opens an accordion between rows), so there is no percentage
  // to compute. Read where the dots actually are.
  const [spineBox, setSpineBox] = useState<
    { top: number; height: number; fill: number } | null
  >(null);

  const measureSpine = useCallback(() => {
    const body = railBodyRef.current;
    const dots = nodeRefs.current.map(
      (node) => node?.querySelector(".mcp-chat-flow-node-dot") ?? null,
    );
    const first = dots[0];
    const last = dots[dots.length - 1];
    const active = dots[activeIndex] ?? first;
    if (!body || !first || !last || !active) {
      setSpineBox(null);
      return;
    }
    // Relative to the rail body's padding box and independent of its scroll
    // offset, so these hold while the reader scrolls a long rail.
    const origin = body.getBoundingClientRect().top - body.scrollTop;
    const centre = (dot: Element) => {
      const rect = dot.getBoundingClientRect();
      return rect.top + rect.height / 2 - origin;
    };
    const top = centre(first);
    const height = centre(last) - top;
    if (!Number.isFinite(top) || !Number.isFinite(height) || height <= 0) {
      setSpineBox(null);
      return;
    }
    setSpineBox({
      top,
      height,
      fill: Math.min(Math.max(centre(active) - top, 0), height),
    });
  }, [activeIndex]);

  useLayoutEffect(() => {
    measureSpine();
  }, [measureSpine, steps, narrow]);

  useLayoutEffect(() => {
    const body = railBodyRef.current;
    if (!body || typeof ResizeObserver === "undefined") {
      return undefined;
    }
    // A wrapped headline, an opened accordion, or the window changing width
    // all move the dots without changing which step is selected.
    const observer = new ResizeObserver(() => measureSpine());
    observer.observe(body);
    for (const node of nodeRefs.current) {
      if (node) {
        observer.observe(node);
      }
    }
    return () => observer.disconnect();
  }, [measureSpine, steps, narrow]);

  useEffect(() => {
    setActiveIndex((current) => Math.min(current, Math.max(steps.length - 1, 0)));
  }, [steps.length]);

  function selectStep(index: number, options: { focus?: boolean; scroll?: boolean } = {}) {
    setActiveIndex(index);
    if (options.focus) {
      nodeRefs.current[index]?.focus();
    }
    if (options.scroll !== false) {
      nodeRefs.current[index]?.scrollIntoView({ block: "nearest" });
    }
  }

  function handleRailKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") {
      return;
    }
    event.preventDefault();
    const delta = event.key === "ArrowDown" ? 1 : -1;
    const next = (activeIndex + delta + steps.length) % steps.length;
    selectStep(next, { focus: true });
  }

  const totalCalls = timeline.length;

  return (
    <section className="feature-section mcp-chat-flow-section">
      <div className="panel-header">
        <div className="panel-title-with-count">
          <span>{t("detail.flow.title")}</span>
        </div>
      </div>
      {timeline.length === 0 ? (
        <p className="page-properties-empty">{t("detail.empty.timeline")}</p>
      ) : steps.length === 0 ? (
        <p className="page-properties-empty">{t("detail.flow.allBootstrap")}</p>
      ) : (
        <div className="mcp-chat-flow-layout">
          <div className="mcp-chat-flow-rail">
            <div className="mcp-chat-flow-rail-head">
              <span>{t("detail.flow.title")}</span>
              <em>
                {t(steps.length === 1 ? "detail.flow.stepCount_one" : "detail.flow.stepCount_other", {
                  count: steps.length,
                })}
                {" · "}
                {totalCalls}
                {" "}
                {t(totalCalls === 1 ? "counts.call_one" : "counts.call_other")}
              </em>
              {!narrow ? (
                <button
                  type="button"
                  className="mcp-chat-flow-jump"
                  onClick={() => selectStep(activeIndex, { scroll: true })}
                >
                  {t("detail.flow.jumpToCurrent")}
                </button>
              ) : null}
            </div>
            <div
              className="mcp-chat-flow-rail-body"
              ref={railBodyRef}
              role={narrow ? undefined : "tablist"}
              aria-orientation={narrow ? undefined : "vertical"}
              aria-label={narrow ? undefined : t("detail.flow.title")}
              onKeyDown={narrow ? undefined : handleRailKeyDown}
            >
              <span
                className="mcp-chat-flow-spine"
                aria-hidden="true"
                style={
                  spineBox
                    ? {
                        top: `${spineBox.top}px`,
                        bottom: "auto",
                        height: `${spineBox.height}px`,
                      }
                    : undefined
                }
              >
                <b style={{ height: spineBox ? `${spineBox.fill}px` : 0 }} />
              </span>
              {steps.map((step, index) => {
                const selected = index === activeIndex;
                const nodeId = `mcp-flow-node-${step.id}`;
                const panelId = `mcp-flow-panel-${step.id}`;
                return (
                  <div className="mcp-chat-flow-row" key={step.id}>
                    <button
                      type="button"
                      id={nodeId}
                      role={narrow ? undefined : "tab"}
                      aria-selected={narrow ? undefined : selected}
                      aria-expanded={narrow ? selected : undefined}
                      aria-controls={panelId}
                      tabIndex={narrow ? undefined : (selected ? 0 : -1)}
                      className={`mcp-chat-flow-node${selected ? " is-active" : ""}${step.hasError ? " is-error" : ""}`}
                      onClick={() => selectStep(index, { scroll: false })}
                      ref={(element) => {
                        nodeRefs.current[index] = element;
                      }}
                    >
                      <FlowNodeBody index={index} step={step} />
                    </button>
                    {narrow && selected ? (
                      <ChatFlowPanel
                        step={step}
                        id={panelId}
                        labelledBy={nodeId}
                        variant="inline"
                        workspaceId={workspaceId}
                      />
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
          {!narrow ? (
            <ChatFlowPanel
              step={steps[activeIndex]}
              id={`mcp-flow-panel-${steps[activeIndex].id}`}
              labelledBy={`mcp-flow-node-${steps[activeIndex].id}`}
              variant="pinned"
              workspaceId={workspaceId}
            />
          ) : null}
        </div>
      )}
    </section>
  );
}

function FlowNodeBody({ index, step }: { index: number; step: ChatFlowStep }) {
  const { t } = useTranslation("chats");
  const representative = chatFlowRepresentativeItem(step.items);
  const subjects = distinctSubjects(step.items);
  const baseTitle = chatPresentationLine(representative, t);
  const title = subjects.length > 1
    ? t("detail.flow.stepTitleMore", { title: baseTitle, count: subjects.length - 1 })
    : baseTitle;
  const latency = totalLatencyMs(step.items);
  const callCount = step.items.length;
  const meta = latency !== null
    ? t(callCount === 1 ? "detail.flow.stepMeta_one" : "detail.flow.stepMeta_other", {
        count: callCount,
        latency: formatLatencyMs(latency),
      })
    : t(callCount === 1 ? "detail.flow.stepMetaNoLatency_one" : "detail.flow.stepMetaNoLatency_other", {
        count: callCount,
      });
  return (
    <>
      <span className="mcp-chat-flow-node-dot">{String(index + 1).padStart(2, "0")}</span>
      <span className="mcp-chat-flow-node-body">
        <span className={`mcp-chat-flow-node-kind mcp-chat-flow-phase-${step.phase}`}>
          {t(`detail.flow.phaseLabel.${step.phase}`)}
        </span>
        <span className="mcp-chat-flow-node-title">{title}</span>
        <span className="mcp-chat-flow-node-meta">{meta}</span>
      </span>
      <span className="mcp-chat-flow-node-check" aria-hidden="true">
        {step.hasError ? <AlertTriangle size={16} /> : <Check size={16} />}
      </span>
    </>
  );
}

const PANEL_HEADING_KEY: Record<ChatFlowPhase, string> = {
  find: "detail.flow.panelHeading.find",
  read: "detail.flow.panelHeading.read",
  write: "detail.flow.panelHeading.write",
  other: "detail.flow.panelHeading.other",
};

function ChatFlowPanel({
  step,
  id,
  labelledBy,
  variant,
  workspaceId,
}: {
  step: ChatFlowStep;
  id: string;
  labelledBy: string;
  variant: "pinned" | "inline";
  workspaceId: string;
}) {
  const { t } = useTranslation("chats");
  const latency = totalLatencyMs(step.items);
  const callCount = step.items.length;
  const callsSummary = latency !== null
    ? t(callCount === 1 ? "detail.flow.stepMeta_one" : "detail.flow.stepMeta_other", {
        count: callCount,
        latency: formatLatencyMs(latency),
      })
    : t(callCount === 1 ? "detail.flow.stepMetaNoLatency_one" : "detail.flow.stepMetaNoLatency_other", {
        count: callCount,
      });

  return (
    <section
      className={`mcp-chat-flow-panel mcp-chat-flow-panel--${variant}`}
      id={id}
      role={variant === "pinned" ? "tabpanel" : undefined}
      aria-labelledby={labelledBy}
      tabIndex={variant === "pinned" ? 0 : undefined}
    >
      <div className="mcp-chat-flow-panel-head">
        <span className={`mcp-chat-flow-chip mcp-chat-flow-phase-${step.phase}`}>
          {t(`detail.flow.phaseLabel.${step.phase}`)}
        </span>
        <strong>{t(PANEL_HEADING_KEY[step.phase])}</strong>
        <span className="mcp-chat-flow-panel-calls">{callsSummary}</span>
      </div>
      {step.items.map((item) => (
        <ChatFlowCall
          key={item.id}
          item={item}
          showTimestamp={step.items.length > 1}
          workspaceId={workspaceId}
        />
      ))}
    </section>
  );
}

function ChatFlowCall({
  item,
  showTimestamp,
  workspaceId,
}: {
  item: McpChatTimelineItem;
  showTimestamp: boolean;
  workspaceId: string;
}) {
  const { t } = useTranslation("chats");
  const failed = isChatFlowError(item.result_status);
  const phase = chatFlowPhaseForAction(item.action);
  const [activeTab, setActiveTab] = useState<TimelineTab>(
    () => defaultCallTab(item, phase, failed),
  );
  const idBase = `mcp-flow-call-${item.id}`;
  const headline = chatPresentationLine(item, t);

  // The tabs sit on the card itself rather than behind a "technical details"
  // disclosure, so the opening tab carries what used to be promoted above it:
  // the call's primary display items. Rendering both would print the same list
  // twice, back to back.
  return (
    <article className={`mcp-chat-flow-call${failed ? " is-error" : ""}`}>
      <div className="mcp-chat-flow-call-head">
        <StatusPill
          status={item.result_status}
          label={statusLabel(item.result_status, t)}
          title={statusLabel(item.result_status, t)}
          compact
        />
        <span className="mcp-chat-flow-call-headline">{headline}</span>
        {item.latency_ms !== null ? (
          <span className="mcp-chat-flow-call-latency">{formatLatencyMs(item.latency_ms)}</span>
        ) : null}
        {showTimestamp ? (
          <span className="mcp-chat-flow-call-time">{formatVersionTimestamp(item.started_at)}</span>
        ) : null}
      </div>
      {phase === "find" && item.subject ? (
        <div className="mcp-chat-flow-query">
          <Search size={15} aria-hidden="true" />
          <span>{item.subject}</span>
        </div>
      ) : null}
      {failed ? <CallFailureNotice item={item} phase={phase} /> : null}
      <ChatEditCard item={item} workspaceId={workspaceId} />
      <ReaderTabs<TimelineTab>
        accent="context"
        active={activeTab}
        ariaLabel={t("detail.timelineItem.details", { title: headline })}
        idBase={idBase}
        onChange={setActiveTab}
        tabs={[
          { key: "inputs", label: t("detail.tabs.inputs") },
          { key: "result", label: t("detail.tabs.result") },
          {
            key: "linked",
            label: t("detail.tabs.linked"),
            count: item.linked_artifacts.length,
          },
          { key: "metadata", label: t("detail.tabs.metadata") },
        ]}
      />
      <div
        className="mcp-tool-tab-panel"
        id={`${idBase}-panel-${activeTab}`}
        role="tabpanel"
        aria-labelledby={`${idBase}-tab-${activeTab}`}
        tabIndex={0}
      >
        <TimelineTabPanel item={item} phase={phase} tab={activeTab} />
      </div>
    </article>
  );
}

function CallFailureNotice({
  item,
  phase,
}: {
  item: McpChatTimelineItem;
  phase: ChatFlowPhase;
}) {
  const { t } = useTranslation("chats");
  const guidance = item.error_code ? errorGuidance(item.error_code, t) : null;
  const message = item.result_summary && !isStatusEcho(item.result_summary, item.result_status)
    ? item.result_summary
    : null;
  // Which operations collided is structure (indexes, line numbers), not page
  // text, so every reader gets it; the server's own wording stays caller-only.
  const conflicts = (item.error_details ?? []).flatMap((detail) =>
    detail.conflict ? [detail.conflict] : [],
  );
  // Where a refused replace_text nearly matched: positions and a score,
  // never text, so like the overlap lines every reader may see them.
  const candidateRows = matchCandidateRows(item.error_details ?? [], t);
  return (
    <div className="mcp-chat-flow-call-failure">
      <p className="mcp-chat-flow-call-failure-head">
        <strong>
          {item.error_code
            ? errorCodeLabel(item.error_code, t)
            : statusLabel(item.result_status, t)}
        </strong>
        {/* Every write path runs in one transaction, so a failed, conflicting
            or refused write left the document exactly as it was. Saying so
            keeps a red card from reading as a half-applied edit. */}
        {phase === "write" ? (
          <span className="mcp-chat-flow-unchanged">{t("detail.failure.unchanged")}</span>
        ) : null}
      </p>
      {guidance ? <p className="mcp-chat-flow-call-guidance">{guidance}</p> : null}
      {conflicts.map((conflict) => (
        <p
          className="mcp-chat-flow-call-guidance"
          key={`${conflict.left_op_index}:${conflict.right_op_index}`}
        >
          {conflict.left_lines && conflict.right_lines
            ? t("detail.failure.overlapLines", {
                left: conflict.left_op_index,
                right: conflict.right_op_index,
                leftLines: lineSpan(conflict.left_lines),
                rightLines: lineSpan(conflict.right_lines),
              })
            : t("detail.failure.overlap", {
                left: conflict.left_op_index,
                right: conflict.right_op_index,
              })}
        </p>
      ))}
      {candidateRows.map((row) => (
        <p className="mcp-chat-flow-call-guidance" key={row.key}>
          {row.text}
        </p>
      ))}
      {message ? <p className="mcp-chat-flow-call-message">{message}</p> : null}
    </div>
  );
}

// Where a refused replace_text points the caller -- positions and a score,
// never text, so like the overlap lines every reader may see them. An
// occurrence-count refusal lists the exact matches it counted (one row per
// operation, each line once); the other refusals list the nearest text they
// did not apply. Rows are deduplicated, so each key is unique.
function matchCandidateRows(
  details: NonNullable<McpChatTimelineItem["error_details"]>,
  t: TFunction<"chats">,
): { key: string; text: string }[] {
  const rows = new Map<string, string>();
  for (const detail of details) {
    const candidates = detail.candidates ?? [];
    if (candidates.length === 0) continue;
    const op = detail.operation_index ?? null;
    if (detail.code === "text_occurrence_mismatch") {
      const lines = [
        ...new Set(candidates.map((c) => lineSpan([c.line_start, c.line_end]))),
      ].join(", ");
      rows.set(
        `occurrences:${op}`,
        op === null
          ? t("detail.failure.occurrences", { lines })
          : t("detail.failure.occurrencesForOperation", { lines, op }),
      );
      continue;
    }
    for (const candidate of candidates) {
      const lines = lineSpan([candidate.line_start, candidate.line_end]);
      const percent = similarityPercent(candidate.similarity);
      rows.set(
        `nearest:${op}:${lines}:${percent}`,
        op === null
          ? t("detail.failure.candidate", { lines, percent })
          : t("detail.failure.candidateForOperation", { lines, percent, op }),
      );
    }
  }
  return [...rows].map(([key, text]) => ({ key, text }));
}

function lineSpan([start, end = start]: number[]) {
  return start === end ? String(start) : `${start}–${end}`;
}

// Open on the tab that carries the call's primary content: what went wrong
// for a failure, what was sent for a write, what came back otherwise. A write
// that recorded no input at all falls back to its result, the same fallback
// the promoted display list used before the tabs moved onto the card.
function defaultCallTab(
  item: McpChatTimelineItem,
  phase: ChatFlowPhase,
  failed: boolean,
): TimelineTab {
  if (failed) {
    return "result";
  }
  if (phase === "write" && hasInputDetail(item)) {
    return "inputs";
  }
  return "result";
}

function hasInputDetail(item: McpChatTimelineItem) {
  return item.input_availability !== "redacted"
    && ((item.input_display_items ?? []).length > 0
      || Object.keys(item.input_summary).length > 0);
}

function ChatDisplayItems({ items }: { items: ChatDisplayItem[] }) {
  const { t } = useTranslation("chats");
  return (
    <dl className="mcp-tool-display-items">
      {items.map((displayItem, index) => {
        const canLink =
          displayItem.kind === "artifact" &&
          Boolean(displayItem.resource_kind && displayItem.resource_id);
        return (
          <div key={`${displayItem.key}:${displayItem.resource_id ?? displayItem.text}:${index}`}>
            <dt>
              {t(`detail.displayFields.${displayItem.key}`, {
                defaultValue: humanizeIdentifier(displayItem.key),
              })}
            </dt>
            <dd>
              {canLink ? (
                // Same chip language as a lint finding's target link (a
                // bordered, icon-led box) instead of a bare underlined
                // hyperlink, so every "this points at a real page" affordance
                // in the product reads the same way.
                <Link
                  className="mcp-chat-dl-link"
                  to={artifactNavigationPath({
                    navigation_path: displayItem.navigation_path ?? null,
                  })}
                >
                  <ExternalLink size={13} aria-hidden="true" />
                  <span>{displayItem.text}</span>
                </Link>
              ) : isResourceTypeField(displayItem.key) || displayItem.key === "page_type" ? (
                <TypeChip value={displayItem.text} pageType={displayItem.key === "page_type"} />
              ) : displayItem.kind === "status" ? (
                // A status item's text is a machine word ("discarded"), not
                // prose, so it is localized like every other status.
                t(`detail.displayStatus.${displayItem.text}`, {
                  defaultValue: humanizeIdentifier(displayItem.text),
                })
              ) : (
                displayItem.text
              )}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

function artifactNavigationPath(artifact: { navigation_path: string | null }) {
  // The server (mcp_chat_service.py's _artifact_title_and_path) already
  // computes this per resource_kind and returns null when the resource is
  // missing or not visible to the requesting actor. Rebuilding a path
  // client-side from resource_kind/resource_id alone would bypass that
  // visibility gate and link to a resource the actor was never shown as
  // accessible, so navigation_path is the only source of truth here.
  return artifact.navigation_path ?? "/chats";
}

function iconForArtifactKind(kind: string): ReactNode {
  if (kind === "context_page") return <FileText size={15} aria-hidden="true" />;
  if (kind === "library_item") return <Book size={15} aria-hidden="true" />;
  if (kind === "agent_page") return <Bot size={15} aria-hidden="true" />;
  if (kind === "context_media_asset") return <ImageIcon size={15} aria-hidden="true" />;
  return <ExternalLink size={15} aria-hidden="true" />;
}

type TimelineTab = "inputs" | "result" | "linked" | "metadata";

type ChatDisplayItem = NonNullable<McpChatTimelineItem["result_display_items"]>[number];

// Input display items the edit card already states in its summary line.
const EDIT_CARD_DISPLAY_KEYS = new Set(["edit_operations", "edit_sections", "edit_chars"]);

// Why a result is hidden decides what the reader is told: a document they
// cannot see, or a server message only the caller may read.
const RESULT_REDACTION_KEY = {
  target_hidden: "detail.availability.result.redactedTargetHidden",
  error_restricted: "detail.availability.result.redactedErrorRestricted",
  default: "detail.availability.result.redacted",
} as const;

function TimelineTabPanel({
  item,
  phase,
  tab,
}: {
  item: McpChatTimelineItem;
  phase: ChatFlowPhase;
  tab: TimelineTab;
}) {
  const { t } = useTranslation("chats");
  if (tab === "inputs") {
    const entries = Object.entries(item.input_summary);
    const hasEditCard = Boolean(item.edit_summary);
    // A search call's own query already renders in the query box above the
    // tabs (the backend always projects it into input_display_items as
    // "search_query" too), and a write's operations/sections/size already
    // head its edit card. Listing either here would repeat it verbatim.
    const displayItems = (item.input_display_items ?? []).filter(
      (candidate) =>
        !(phase === "find" && candidate.key === "search_query")
        && !(hasEditCard && EDIT_CARD_DISPLAY_KEYS.has(candidate.key)),
    );
    if (item.input_availability === "redacted") {
      return (
        <p className="page-properties-empty mcp-tool-panel-card">
          {item.redaction_reason === "target_hidden"
            ? t("detail.availability.input.redactedTargetHidden")
            : t("detail.availability.input.redacted")}
        </p>
      );
    }
    // A failed call on a document this reader cannot see keeps only the shape
    // of its request (field names, op kinds, revision). Say why it is thin.
    const shapeOnlyNote = item.redaction_reason === "target_hidden" ? (
      <p className="page-properties-empty mcp-tool-inputs-note">
        {t("detail.availability.input.shapeOnly")}
      </p>
    ) : null;
    if (displayItems.length === 0 && entries.length === 0) {
      return (
        <p className="page-properties-empty mcp-tool-panel-card">
          {shapeOnlyNote
            ? t("detail.availability.input.shapeOnly")
            : t(`detail.availability.input.${item.input_availability}`, {
                defaultValue: t("detail.empty.inputs"),
              })}
        </p>
      );
    }
    // Long arguments get their own full-width block. In the shared grid a
    // megabyte of markdown was laid out in a ~120px column and ran on for
    // thousands of lines, burying every other argument.
    const references = entries.filter(([key, value]) =>
      key === "expected_editing_revision"
      || (key !== "workspace_id" && isIdentifierField(key) && typeof value === "string"),
    );
    const ordinary = entries.filter((entry) => !references.includes(entry));
    const shortEntries = ordinary.filter(([, value]) => !isLongToolValue(value));
    shortEntries.sort(([a], [b]) => Number(b === "workspace_id") - Number(a === "workspace_id"));
    const longEntries = ordinary.filter(([, value]) => isLongToolValue(value));
    const inputLabel = (key: string) => key === "expected_editing_revision" ? key : chatInputLabel(key, t);
    return (
      <div className="mcp-tool-panel-card">
        {shapeOnlyNote}
        {displayItems.length > 0 ? (
          <ChatDisplayItems items={displayItems} />
        ) : null}
        {entries.length > 0 ? (
          <section className="mcp-tool-technical-inputs">
            <h4>{t("detail.technicalInputs")}</h4>
            <div className="mcp-chat-input-grid">
              {shortEntries.length > 0 ? (
                <dl className="mcp-tool-result-meta mcp-chat-input-fields">
                  {shortEntries.map(([key, value]) => (
                    <div key={key}>
                      <dt>{inputLabel(key)}</dt>
                      <dd>
                        {key === "workspace_id" && typeof value === "string" ? (
                          <WorkspaceInput value={value} />
                        ) : (isResourceTypeField(key) || key === "page_type") && typeof value === "string" ? (
                          <TypeChip value={value} pageType={key === "page_type"} />
                        ) : formatSummaryValue(value)}
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : null}
              {longEntries.map(([key, value]) => (
                <ToolInputValue key={key} label={inputLabel(key)} value={value} />
              ))}
              {references.length > 0 ? (
                <details className="mcp-chat-input-reference-details">
                  <summary>{t("detail.identifier.rawReferences")}</summary>
                  <dl className="mcp-tool-result-meta mcp-chat-input-identifiers">
                    {references.map(([key, value]) => (
                      <div key={key}>
                        <dt>{inputLabel(key)}</dt>
                        <dd>
                          {isIdentifierField(key) && typeof value === "string" ? (
                            <ChatIdentifier value={value} label={inputLabel(key)} />
                          ) : formatSummaryValue(value)}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </details>
              ) : null}
            </div>
          </section>
        ) : null}
      </div>
    );
  }
  if (tab === "result") {
    // Status, error code, latency and times are already on the card head,
    // in its failure notice, and in Metadata. Repeating them here put a
    // four-cell grid on every card once the tabs stopped being collapsed.
    const failed = isChatFlowError(item.result_status);
    // A failed call's server message is printed in the failure notice above
    // the tabs (for the caller the server also projects it as a "summary"
    // item), so it is not repeated here.
    const displayItems = (item.result_display_items ?? []).filter(
      (candidate) => !(failed && candidate.key === "summary"),
    );
    const hasSummary =
      displayItems.length === 0 &&
      !failed &&
      Boolean(item.result_summary) &&
      !isStatusEcho(item.result_summary, item.result_status);
    return (
      <div className="mcp-tool-result mcp-tool-panel-card">
        {item.result_availability === "redacted" ? (
          <p className="page-properties-empty">
            {t(RESULT_REDACTION_KEY[item.redaction_reason ?? "default"])}
          </p>
        ) : displayItems.length > 0 ? (
          <ChatDisplayItems items={displayItems} />
        ) : hasSummary ? (
          <p className="mcp-tool-result-summary">{item.result_summary}</p>
        ) : (
          <p className="page-properties-empty">
            {t(`detail.availability.result.${item.result_availability}`, {
              defaultValue: t("detail.empty.result"),
            })}
          </p>
        )}
      </div>
    );
  }
  if (tab === "linked") {
    if (item.linked_artifacts.length === 0) {
      return (
        <p className="page-properties-empty mcp-tool-panel-card">
          {t("detail.empty.linked")}
        </p>
      );
    }
    return (
      <div className="mcp-tool-linked mcp-tool-panel-card">
        <span className="mcp-tool-linked-label">
          {t("detail.linkedLabel")}
        </span>
        <div className="mcp-artifact-strip">
          {item.linked_artifacts.map((artifact) => (
            <Link
              className="mcp-artifact-chip"
              data-kind={artifact.resource_kind}
              key={`${artifact.resource_kind}:${artifact.resource_id}:${artifact.verb}`}
              to={artifactNavigationPath(artifact)}
            >
              <span className="mcp-artifact-chip-icon" aria-hidden="true">
                {iconForArtifactKind(artifact.resource_kind)}
              </span>
              <span className="mcp-artifact-chip-body">
                <strong>
                  {artifact.artifact_title
                    ?? t(`labels.artifactKind.${artifact.resource_kind}`, {
                      defaultValue: t("labels.artifactKind.unknown"),
                    })}
                </strong>
                <span className="mcp-artifact-chip-meta">
                  <StatusPill
                    status={artifact.verb}
                    label={artifactVerbLabel(artifact.verb, t)}
                    title={artifactVerbLabel(artifact.verb, t)}
                    compact
                  />
                </span>
              </span>
            </Link>
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className="mcp-tool-panel-card">
      <dl className="mcp-tool-result-meta mcp-chat-metadata-grid">
        <div>
          <dt>{t("detail.fields.status")}</dt>
          <dd>{statusLabel(item.result_status, t)}</dd>
        </div>
        <div>
          <dt>{t("detail.fields.started")}</dt>
          <dd>{formatVersionTimestamp(item.started_at)}</dd>
        </div>
        <div>
          <dt>{t("detail.fields.completed")}</dt>
          <dd>
            {item.completed_at ? formatVersionTimestamp(item.completed_at) : "—"}
          </dd>
        </div>
        <div>
          <dt>{t("units.latency")}</dt>
          <dd>{item.latency_ms !== null ? formatLatencyMs(item.latency_ms) : "—"}</dd>
        </div>
        <div>
          <dt>{t("detail.fields.error")}</dt>
          <dd>{item.error_code ? errorCodeLabel(item.error_code, t) : "—"}</dd>
        </div>
        <div>
          <dt>{t("detail.fields.schema")}</dt>
          <dd>{schemaVersion(item.schema_version)}</dd>
        </div>
        <div className="mcp-chat-call-headline">
          <dt>{t("detail.fields.headline")}</dt>
          <dd>{item.headline}</dd>
        </div>
        <div>
          <dt>{t("detail.fields.id")}</dt>
          <dd><ChatIdentifier value={item.id} label={t("detail.fields.id")} /></dd>
        </div>
      </dl>
    </div>
  );
}

// A result_summary that merely restates the status ("Tool call succeeded.")
// is redundant with the StatusPill, so it is treated as "no real summary".
function isStatusEcho(summary: string | null, status: string) {
  if (!summary) {
    return true;
  }
  const trimmed = summary.trim();
  if (/^tool call (succeeded|failed|completed)\.?$/i.test(trimmed)) {
    return true;
  }
  return trimmed.toLowerCase() === status.replace(/_/g, " ").toLowerCase();
}

function formatSummaryValue(value: unknown): string {
  // Nested objects/arrays from raw MCP tool arguments would render as
  // "[object Object]" via String(); serialize them compactly instead (C13).
  if (value !== null && typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

const KNOWN_CHAT_STATUSES = new Set([
  "accepted",
  "succeeded",
  "partial",
  "needs_review",
  "failed",
  "rejected_by_policy",
  "conflict",
  "pending",
  "running",
]);

function statusLabel(status: string, t: TFunction<"chats">): string {
  const normalized = status.trim().toLowerCase();
  return KNOWN_CHAT_STATUSES.has(normalized)
    ? t(`detail.statuses.${normalized}`)
    : t("detail.statuses.unknown");
}

// Error codes with a localized label. Keep in step with the codes the MCP
// envelope emits (backend/mcp/envelope.py ERROR_CODE_GUIDANCE) that a reader
// of Chats is likely to meet; the rest fall through to the raw code below.
const KNOWN_ERROR_CODES = new Set([
  "tool_failed",
  "internal_error",
  "rejected_by_policy",
  "conflict",
  "validation_failed",
  "stale_base_version",
  "operation_unsupported",
  "operations_overlap",
  "block_reflow",
  "text_occurrence_mismatch",
  "text_match_not_found",
  "text_match_ambiguous",
  "text_match_low_confidence",
  "text_not_found",
  "block_not_found",
  "block_hash_mismatch",
  "section_not_found",
  "scope_denied",
  "not_found",
  "workspace_role_insufficient",
]);

// Codes whose label alone does not say what the agent should do next.
const ERROR_CODES_WITH_GUIDANCE = new Set([
  "rejected_by_policy",
  "conflict",
  "validation_failed",
  "stale_base_version",
  "operation_unsupported",
  "operations_overlap",
  "block_reflow",
  "text_occurrence_mismatch",
  "text_match_not_found",
  "text_match_ambiguous",
  "text_match_low_confidence",
  "text_not_found",
  "block_not_found",
  "block_hash_mismatch",
  "section_not_found",
  "scope_denied",
  "not_found",
  "workspace_role_insufficient",
]);

// A machine error code: lowercase snake_case, as every code the envelope
// emits is. Anything else (a request id, free text) is not echoed.
const ERROR_CODE_SHAPE = /^[a-z][a-z0-9_]{0,63}$/;

function errorCodeLabel(errorCode: string, t: TFunction<"chats">): string {
  const code = errorCode.trim();
  if (KNOWN_ERROR_CODES.has(code)) {
    return t(`detail.errorCodes.${code}`);
  }
  // A code this build has no label for is still a stable identifier the
  // reader can search for or quote to an operator, which "Tool call error"
  // was not.
  return ERROR_CODE_SHAPE.test(code) ? code : t("detail.errorCodes.unknown");
}

function errorGuidance(errorCode: string, t: TFunction<"chats">): string | null {
  const code = errorCode.trim();
  return ERROR_CODES_WITH_GUIDANCE.has(code) ? t(`detail.errorGuidance.${code}`) : null;
}

function durationBetween(start: string, end: string) {
  const startTime = new Date(start).getTime();
  const endTime = new Date(end).getTime();
  if (Number.isNaN(startTime) || Number.isNaN(endTime) || endTime < startTime) {
    return null;
  }
  return endTime - startTime;
}
