import { Archive, ArchiveRestore, Hourglass, Info, Mail, MailOpen } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { apiErrorMessage } from "../../shared/api/client";
import {
  ACCOUNT_INBOX_SCOPE,
  listInbox,
  markInboxItem,
  workspaceInboxScope,
  type InboxItem,
  type InboxPage,
  type InboxState,
} from "../../shared/api/inbox";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { formatRelativeTimestamp } from "../../shared/lib/datetime";
import { parseWorkspaceRoute, workspacePath } from "../../shared/lib/workspaceRoutes";
import { presentInboxItem } from "./inboxRenderers";
import { inboxVisibleUntil } from "./inboxVisibility";
import { inboxScopeOptions } from "../../shared/inbox/inboxScopes";
import { notifyInboxChanged } from "../../shared/inbox/useInboxUnread";

function itemKey(item: InboxItem) {
  return `${item.kind}:${item.item_key}`;
}

function inView(item: InboxItem, state: InboxState) {
  if (state === "archived") return item.archived;
  return !item.archived && (state === "all" || item.unread);
}

function withItem(page: InboxPage, next: InboxItem, state: InboxState): InboxPage {
  const replace = (items: InboxItem[]) =>
    items.map((item) => (itemKey(item) === itemKey(next) ? next : item)).filter((item) => inView(item, state));
  return { ...page, status_items: replace(page.status_items), items: replace(page.items) };
}

// Short within the current year; the year appears once an item is older.
function formatItemTime(date: Date, locale: string) {
  const year = date.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
  return new Intl.DateTimeFormat(locale, { year, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    .format(date);
}

// When an item leaves on its own: a dated chip when the server states the end,
// otherwise an info button. Either one opens the kind's full condition in the
// row, so the explanation also works on touch screens without a hover tooltip.
function InboxRemovalToggle({
  until,
  detail,
  detailId,
  open,
  onToggle,
}: {
  until: Date | null;
  detail: string;
  detailId: string;
  open: boolean;
  onToggle: () => void;
}) {
  const { t, i18n } = useTranslation("inbox");
  return (
    <button
      aria-controls={detailId}
      aria-expanded={open}
      aria-label={until ? undefined : t("removal.label")}
      className={until ? "inbox-removal-chip" : "inbox-removal-info"}
      onClick={onToggle}
      title={detail}
      type="button"
    >
      {until ? (
        <>
          <Hourglass size={12} aria-hidden="true" />
          {t("removal.chip", {
            relative: formatRelativeTimestamp(until.toISOString(), i18n.language, { alwaysRelative: true }),
          })}
        </>
      ) : (
        <Info size={14} aria-hidden="true" />
      )}
    </button>
  );
}

export function InboxItemRow({
  item,
  busy,
  onMark,
}: {
  item: InboxItem;
  busy: boolean;
  onMark: (item: InboxItem, marks: { read?: boolean; archived?: boolean }) => void;
}) {
  const { t, i18n } = useTranslation("inbox");
  const { title, summary, removal, icon: Icon } = presentInboxItem(item, t);
  const href = item.display?.href;
  const time = item.event_at
    ? formatItemTime(new Date(item.event_at), i18n.language)
    : null;
  const until = inboxVisibleUntil(item);
  const [removalOpen, setRemovalOpen] = useState(false);
  const detailId = useId();
  const removalDetail = removal && until ? `${t("removal.until", { date: until })} ${removal}` : removal;
  const removalToggle = removalDetail ? (
    <InboxRemovalToggle
      detail={removalDetail}
      detailId={detailId}
      onToggle={() => setRemovalOpen((open) => !open)}
      open={removalOpen}
      until={until}
    />
  ) : null;
  // A dated end or a summary gets its own line; a bare condition sits by the title.
  const removalInMeta = summary !== null || until !== null;
  const readLabel = item.unread ? t("actions.markRead") : t("actions.markUnread");
  const archiveLabel = item.archived ? t("actions.unarchive") : t("actions.archive");
  return (
    <li className={`inbox-row${item.unread ? " is-unread" : ""}${item.archived ? " is-archived" : ""}`}>
      <span className="inbox-row-dot" role="img" aria-label={item.unread ? t("item.unread") : t("item.read")} />
      <div className="inbox-row-body">
        <div className="inbox-row-head">
          {Icon ? <Icon className="inbox-row-icon" size={15} aria-hidden="true" /> : null}
          {href ? (
            <Link className="inbox-row-title" to={href} onClick={() => item.unread && onMark(item, { read: true })}>
              {title}
            </Link>
          ) : (
            <span className="inbox-row-title">{title}</span>
          )}
          {removalInMeta ? null : removalToggle}
          {time ? <time className="inbox-row-time" dateTime={item.event_at ?? undefined}>{time}</time> : null}
        </div>
        {summary || (removalInMeta && removalToggle) ? (
          <div className="inbox-row-meta">
            {summary ? <span className="inbox-row-summary">{summary}</span> : null}
            {removalInMeta ? removalToggle : null}
          </div>
        ) : null}
        {removalDetail ? (
          <p className="inbox-removal-detail" hidden={!removalOpen} id={detailId}>
            {removalDetail}
          </p>
        ) : null}
      </div>
      <div className="inbox-row-actions">
        <button
          aria-label={readLabel}
          className="icon-button"
          disabled={busy}
          onClick={() => onMark(item, { read: item.unread })}
          title={readLabel}
          type="button"
        >
          {item.unread ? <MailOpen size={16} aria-hidden="true" /> : <Mail size={16} aria-hidden="true" />}
        </button>
        <button
          aria-label={archiveLabel}
          className="icon-button"
          disabled={busy}
          onClick={() => onMark(item, { archived: !item.archived })}
          title={archiveLabel}
          type="button"
        >
          {item.archived ? <ArchiveRestore size={16} aria-hidden="true" /> : <Archive size={16} aria-hidden="true" />}
        </button>
      </div>
    </li>
  );
}

export function InboxScreen() {
  const { t } = useTranslation("inbox");
  const workspaceContext = useOptionalWorkspaceContext();
  const location = useLocation();
  const navigate = useNavigate();
  const route = parseWorkspaceRoute(location.pathname);
  const routeWorkspaceId = route?.section === "inbox" ? route.workspaceId : null;
  const routeWorkspace = routeWorkspaceId
    ? workspaceContext?.workspaces.find((workspace) => workspace.id === routeWorkspaceId) ?? null
    : null;
  // On the account route, an existing membership is offered only as a
  // navigation choice. List/mark requests use the account endpoint until the
  // URL commits to the explicit workspace inbox route.
  const workspaceChoice = routeWorkspaceId
    ? routeWorkspace
    : workspaceContext?.selectedWorkspace ?? null;
  const scopes = inboxScopeOptions({ workspace: workspaceChoice, t });
  // Keep the request scope derived from the URL even while membership data is
  // unavailable. The shell boundary controls access; this screen never
  // silently substitutes the account inbox for an explicit workspace URL.
  const activeScope = routeWorkspaceId
    ? workspaceInboxScope(routeWorkspaceId)
    : ACCOUNT_INBOX_SCOPE;
  const [state, setState] = useState<InboxState>("all");
  const [page, setPage] = useState<InboxPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const request = ++requestRef.current;
    setError(null);
    try {
      const next = await listInbox(activeScope, { state });
      if (request === requestRef.current) setPage(next);
    } catch (caught) {
      if (request === requestRef.current) setError(apiErrorMessage(caught));
    }
    // scope.key identifies the scope; the object itself is rebuilt each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeScope.key, state]);

  useEffect(() => {
    setPage(null);
    void load();
  }, [load]);

  async function loadMore() {
    if (!page?.next_cursor) return;
    const request = requestRef.current;
    setLoadingMore(true);
    try {
      const next = await listInbox(activeScope, { state, cursor: page.next_cursor });
      if (request !== requestRef.current) return;
      setPage({ ...page, items: [...page.items, ...next.items], next_cursor: next.next_cursor });
    } catch (caught) {
      if (request === requestRef.current) setError(apiErrorMessage(caught));
    } finally {
      setLoadingMore(false);
    }
  }

  async function mark(item: InboxItem, marks: { read?: boolean; archived?: boolean }) {
    const request = requestRef.current;
    setBusyKey(itemKey(item));
    try {
      // The response carries the item's current state, including a newer
      // revision when the mark was not applied, so loaded pages stay in place.
      const result = await markInboxItem(activeScope, item, marks);
      notifyInboxChanged();
      if (request === requestRef.current) {
        setPage((current) => current && withItem(current, result.item, state));
      }
    } catch (caught) {
      if (request === requestRef.current) setError(apiErrorMessage(caught));
    } finally {
      setBusyKey(null);
    }
  }

  const rows = (items: InboxItem[]) => (
    <ul className="inbox-list">
      {items.map((item) => (
        <InboxItemRow
          busy={busyKey === itemKey(item)}
          item={item}
          key={itemKey(item)}
          onMark={(target, marks) => void mark(target, marks)}
        />
      ))}
    </ul>
  );
  const nothingLoaded = page !== null && page.items.length === 0 && page.status_items.length === 0;
  // A bounded scan can find nothing to show yet still leave older entries.
  const nothingShown = nothingLoaded && page.status_overflow.length === 0 && !page.next_cursor;
  const scanEmpty = nothingLoaded && Boolean(page.next_cursor);
  const groupTitle = (label: string, count: number, more: boolean, hint?: string) => (
    <h2 className="inbox-group-title">
      {label}
      <CountChip approxMore={more} compact value={count} />
      {hint ? <span className="inbox-group-hint">{hint}</span> : null}
    </h2>
  );

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main inbox-screen">
        <Breadcrumbs items={[{ label: t("title") }]} />
        <div className="feature-header">
          <div>
            <h1>{t("title")}</h1>
            <p className="feature-subtitle">{t("description")}</p>
          </div>
        </div>
        <div className="inbox-controls">
          <SegmentedControl
            label={t("scope.label")}
            onChange={(key) => {
              if (key === "account") {
                navigate("/inbox");
              } else if (workspaceChoice) {
                navigate(workspacePath(workspaceChoice.id, "inbox"));
              }
            }}
            options={scopes.map((option) => ({
              key: option.scope.key,
              // The space keeps the accessible name "Workspace <title>".
              label: option.detail ? (
                <>
                  {option.label}{" "}
                  <span className="inbox-scope-detail">{option.detail}</span>
                </>
              ) : (
                option.label
              ),
              title: option.detail,
            }))}
            value={activeScope.key}
          />
          <SegmentedControl
            compact
            label={t("state.label")}
            onChange={setState}
            options={[
              { key: "all", label: t("state.all") },
              { key: "unread", label: t("state.unread") },
              { key: "archived", label: t("state.archived") },
            ]}
            value={state}
          />
        </div>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        {!page ? (
          error ? null : <RouteLoading label={t("loading")} />
        ) : (
          <>
            {state === "archived" ? (
              <p className="inbox-note">
                <Info size={14} aria-hidden="true" />
                {t("archivedNote")}
              </p>
            ) : null}
            {nothingShown ? <p className="empty-state">{t(`empty.${state}`)}</p> : null}
            {page.status_items.length > 0 || page.status_overflow.length > 0 ? (
              <section className="feature-section inbox-group" aria-label={t("status.label")}>
                {groupTitle(
                  t("status.label"),
                  page.status_items.length,
                  page.status_overflow.length > 0,
                  t("status.hint"),
                )}
                {page.status_items.length > 0 ? rows(page.status_items) : null}
                {page.status_overflow.map((overflow) => (
                  <p className="inbox-overflow" key={overflow.kind}>
                    {t("status.overflow", { total: overflow.total })}
                  </p>
                ))}
              </section>
            ) : null}
            {page.items.length > 0 ? (
              <section className="feature-section inbox-group" aria-label={t("items.label")}>
                {/* Archived lists every kind together, newest archive first. */}
                {state === "archived" ? null : groupTitle(t("items.label"), page.items.length, Boolean(page.next_cursor))}
                {rows(page.items)}
              </section>
            ) : null}
            {scanEmpty ? <p className="inbox-scan-empty">{t("scanEmpty")}</p> : null}
            {page.next_cursor ? (
              <div className="load-more-row">
                <button
                  className="icon-button text-button"
                  disabled={loadingMore}
                  onClick={() => void loadMore()}
                  type="button"
                >
                  {loadingMore ? t("loading") : scanEmpty ? t("checkOlder") : t("loadMore")}
                </button>
              </div>
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}
