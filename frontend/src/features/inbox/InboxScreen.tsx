import { Archive, ArchiveRestore, Circle, CircleDot, Mail, MailOpen } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
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
import { RouteLoading } from "../../shared/components/RouteLoading";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { parseWorkspaceRoute, workspacePath } from "../../shared/lib/workspaceRoutes";
import { presentInboxItem } from "./inboxRenderers";
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

export function InboxItemRow({
  item,
  busy,
  onMark,
  archivable = true,
}: {
  item: InboxItem;
  busy: boolean;
  onMark: (item: InboxItem, marks: { read?: boolean; archived?: boolean }) => void;
  // A status item is current state: it leaves when resolved, so it has no archive.
  archivable?: boolean;
}) {
  const { t, i18n } = useTranslation("inbox");
  const { title, summary, icon: Icon } = presentInboxItem(item, t);
  const href = item.display?.href;
  const time = item.event_at
    ? new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium", timeStyle: "short" }).format(
        new Date(item.event_at),
      )
    : null;
  return (
    <li className={`inbox-row${item.unread ? " is-unread" : ""}`}>
      <span className="inbox-row-dot" aria-label={item.unread ? t("item.unread") : t("item.read")}>
        {item.unread ? <CircleDot size={10} aria-hidden="true" /> : <Circle size={10} aria-hidden="true" />}
      </span>
      <div className="inbox-row-body">
        {Icon ? <Icon className="inbox-row-icon" size={14} aria-hidden="true" /> : null}
        {href ? (
          <Link className="inbox-row-title" to={href} onClick={() => item.unread && onMark(item, { read: true })}>
            {title}
          </Link>
        ) : (
          <span className="inbox-row-title">{title}</span>
        )}
        {summary ? <span className="inbox-row-summary">{summary}</span> : null}
        {time ? <time className="inbox-row-time" dateTime={item.event_at ?? undefined}>{time}</time> : null}
      </div>
      <div className="inbox-row-actions">
        <button
          aria-label={item.unread ? t("actions.markRead") : t("actions.markUnread")}
          className="icon-button"
          disabled={busy}
          onClick={() => onMark(item, { read: item.unread })}
          title={item.unread ? t("actions.markRead") : t("actions.markUnread")}
          type="button"
        >
          {item.unread ? <MailOpen size={16} aria-hidden="true" /> : <Mail size={16} aria-hidden="true" />}
        </button>
        {archivable ? (
          <button
            aria-label={item.archived ? t("actions.unarchive") : t("actions.archive")}
            className="icon-button"
            disabled={busy}
            onClick={() => onMark(item, { archived: !item.archived })}
            title={item.archived ? t("actions.unarchive") : t("actions.archive")}
            type="button"
          >
            {item.archived ? <ArchiveRestore size={16} aria-hidden="true" /> : <Archive size={16} aria-hidden="true" />}
          </button>
        ) : null}
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

  const rows = (items: InboxItem[], archivable = true) => (
    <ul className="inbox-list">
      {items.map((item) => (
        <InboxItemRow
          archivable={archivable}
          busy={busyKey === itemKey(item)}
          item={item}
          key={itemKey(item)}
          onMark={(target, marks) => void mark(target, marks)}
        />
      ))}
    </ul>
  );
  // Nothing to list can still leave an overflow count or a cursor to follow.
  const nothingShown = page && page.items.length === 0 && page.status_items.length === 0;

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
            options={scopes.map((option) => ({ key: option.scope.key, label: option.label }))}
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
            {nothingShown ? <p className="empty-state">{t(`empty.${state}`)}</p> : null}
            {page.status_items.length > 0 || page.status_overflow.length > 0 ? (
              <section className="feature-section inbox-status" aria-label={t("status.label")}>
                {page.status_items.length > 0 ? rows(page.status_items, false) : null}
                {page.status_overflow.map((overflow) => (
                  <p className="inbox-overflow" key={overflow.kind}>
                    {t("status.overflow", { total: overflow.total })}
                  </p>
                ))}
              </section>
            ) : null}
            {page.items.length > 0 ? (
              <section className="feature-section" aria-label={t("items.label")}>
                {rows(page.items)}
              </section>
            ) : null}
            {page.next_cursor ? (
              <div className="load-more-row">
                <button
                  className="icon-button text-button"
                  disabled={loadingMore}
                  onClick={() => void loadMore()}
                  type="button"
                >
                  {loadingMore ? t("loading") : t("loadMore")}
                </button>
              </div>
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}
