import { Archive, ArchiveRestore, Hourglass, Info, Mail, MailOpen } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { apiErrorMessage } from "../../shared/api/client";
import {
  ACCOUNT_INBOX_SCOPE,
  INBOX_PAGE_SIZE,
  listInbox,
  markInboxItem,
  workspaceInboxScope,
  type InboxItem,
  type InboxItemTypeFilter,
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
  return JSON.stringify([item.kind, item.item_key]);
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

function appendItems(current: InboxItem[], next: InboxItem[]) {
  const seen = new Set(current.map(itemKey));
  return [...current, ...next.filter((item) => {
    const key = itemKey(item);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  })];
}

function appendPage(current: InboxPage, next: InboxPage): InboxPage {
  return {
    ...current,
    status_items: appendItems(current.status_items, next.status_items),
    items: appendItems(current.items, next.items),
    status_overflow: next.status_overflow,
    next_cursor: next.next_cursor,
  };
}

function descendingText(left: string, right: string) {
  const leftPoints = Array.from(left);
  const rightPoints = Array.from(right);
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index++) {
    const difference = rightPoints[index].codePointAt(0)! - leftPoints[index].codePointAt(0)!;
    if (difference) return difference;
  }
  return rightPoints.length - leftPoints.length;
}

function eventMicroseconds(value: string) {
  const fraction = (value.match(/\.(\d+)/)?.[1] ?? "").padEnd(6, "0");
  return BigInt(Date.parse(value)) * 1_000n + BigInt(fraction.slice(3, 6));
}

function newestFirst(left: InboxItem, right: InboxItem) {
  if (left.event_at && right.event_at) {
    const difference = eventMicroseconds(right.event_at) - eventMicroseconds(left.event_at);
    if (difference) return difference > 0n ? 1 : -1;
  } else if (left.event_at || right.event_at) {
    return left.event_at ? -1 : 1;
  }
  return descendingText(left.kind, right.kind) || descendingText(left.item_key, right.item_key);
}

// A stated deadline is checked when it passes, then again after these delays
// while the server still lists the item (its clock may run behind ours).
const EXPIRY_REFRESH_DELAYS_MS = [0, 5_000, 30_000, 120_000];

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
  // Only the label ticks; list requests are scheduled separately at expiry.
  const [now, setNow] = useState(() => Date.now());
  const untilAt = until?.getTime();
  const interval = untilAt !== undefined && Math.abs(untilAt - now) <= 60_000 ? 1_000 : 30_000;
  useEffect(() => {
    if (untilAt === undefined) return;
    const timer = window.setInterval(() => setNow(Date.now()), interval);
    return () => window.clearInterval(timer);
  }, [untilAt, interval]);
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
  const [itemType, setItemType] = useState<InboxItemTypeFilter>("all");
  const [page, setPage] = useState<InboxPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [busyKeys, setBusyKeys] = useState<Set<string>>(() => new Set());
  const busyKeysRef = useRef(new Set<string>());
  const loadingMoreRef = useRef(false);
  const refreshingRef = useRef(false);
  const requestRef = useRef(0);
  const refreshRef = useRef(0);
  const loadedPagesRef = useRef(1);
  // Refreshes already made for each stated deadline (ms) in this view.
  const expiryAttemptsRef = useRef(new Map<number, number>());

  const load = useCallback(async () => {
    const request = ++requestRef.current;
    ++refreshRef.current;
    loadedPagesRef.current = 1;
    expiryAttemptsRef.current = new Map();
    setLoadingMore(false);
    setRefreshing(false);
    loadingMoreRef.current = false;
    refreshingRef.current = false;
    busyKeysRef.current = new Set();
    setBusyKeys(new Set());
    setError(null);
    try {
      const next = await listInbox(activeScope, { state, itemType, limit: INBOX_PAGE_SIZE });
      if (request === requestRef.current) setPage(next);
    } catch (caught) {
      if (request === requestRef.current) setError(apiErrorMessage(caught));
    }
    // scope.key identifies the scope; the object itself is rebuilt each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeScope.key, state, itemType]);

  useEffect(() => {
    setPage(null);
    void load();
    return () => {
      requestRef.current += 1;
      refreshRef.current += 1;
    };
  }, [load]);

  // Refresh every already-loaded page without sending the reader back to
  // the first twenty items after a mark or a stated expiry.
  const refresh = useCallback(async () => {
    const request = requestRef.current;
    const revision = ++refreshRef.current;
    const pageCount = loadedPagesRef.current;
    const current = () => request === requestRef.current && revision === refreshRef.current;
    refreshingRef.current = true;
    loadingMoreRef.current = false;
    setRefreshing(true);
    setLoadingMore(false);
    setError(null);
    try {
      let next = await listInbox(activeScope, { state, itemType, limit: INBOX_PAGE_SIZE });
      let loaded = 1;
      while (current() && loaded < pageCount && next.next_cursor) {
        const more = await listInbox(activeScope, { state, itemType, cursor: next.next_cursor, limit: INBOX_PAGE_SIZE });
        next = appendPage(next, more);
        loaded++;
      }
      if (!current()) return false;
      loadedPagesRef.current = loaded;
      setPage(next);
      return true;
    } catch (caught) {
      if (current()) setError(apiErrorMessage(caught));
      return false;
    } finally {
      if (current()) {
        refreshingRef.current = false;
        setRefreshing(false);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeScope.key, state, itemType]);

  useEffect(() => {
    if (!page || refreshing || loadingMore || busyKeys.size > 0) return;
    const attempts = expiryAttemptsRef.current;
    const pending = [...new Set(
      [...page.status_items, ...page.items]
        .map((item) => inboxVisibleUntil(item)?.getTime())
        .filter((at): at is number => at !== undefined),
    )]
      .filter((at) => (attempts.get(at) ?? 0) < EXPIRY_REFRESH_DELAYS_MS.length)
      .map((at) => ({ at, fireAt: at + EXPIRY_REFRESH_DELAYS_MS[attempts.get(at) ?? 0] }));
    if (pending.length === 0) return;
    const fireAt = Math.min(...pending.map((deadline) => deadline.fireAt));
    let timer: number;
    function schedule() {
      // setTimeout overflows beyond about 25 days. Recheck a distant deadline
      // locally, and only query the server once its stated time is reached.
      const remaining = fireAt - Date.now();
      timer = window.setTimeout(() => {
        if (Date.now() < fireAt) {
          schedule();
          return;
        }
        // Count this refresh for every deadline it covers. A server clock
        // behind the browser keeps the item a little longer, so the next
        // attempt follows a delay; after the last one the deadline is left
        // alone, and a failed request cannot loop either.
        for (const deadline of pending) {
          if (deadline.fireAt <= fireAt) attempts.set(deadline.at, (attempts.get(deadline.at) ?? 0) + 1);
        }
        void refresh().then((applied) => { if (applied) notifyInboxChanged(); });
      }, Math.max(0, Math.min(remaining, 2_147_483_647)));
    }
    schedule();
    return () => window.clearTimeout(timer);
  }, [page, refresh, refreshing, loadingMore, busyKeys]);

  async function loadMore() {
    if (!page?.next_cursor || refreshingRef.current || loadingMoreRef.current || busyKeysRef.current.size > 0) return;
    const request = requestRef.current;
    const revision = refreshRef.current;
    const cursor = page.next_cursor;
    const current = () => request === requestRef.current && revision === refreshRef.current;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    setError(null);
    try {
      const next = await listInbox(activeScope, { state, itemType, cursor, limit: INBOX_PAGE_SIZE });
      if (!current()) return;
      loadedPagesRef.current++;
      setPage((loaded) => loaded?.next_cursor === cursor ? appendPage(loaded, next) : loaded);
    } catch (caught) {
      if (current()) setError(apiErrorMessage(caught));
    } finally {
      if (current()) {
        loadingMoreRef.current = false;
        setLoadingMore(false);
      }
    }
  }

  async function mark(item: InboxItem, marks: { read?: boolean; archived?: boolean }) {
    const request = requestRef.current;
    const key = itemKey(item);
    if (busyKeysRef.current.has(key)) return;
    // Opening a title still marks it read. Supersede any older list read
    // before marking so its response cannot restore the item's former state.
    refreshRef.current += 1;
    loadingMoreRef.current = false;
    refreshingRef.current = false;
    setLoadingMore(false);
    setRefreshing(false);
    busyKeysRef.current.add(key);
    setBusyKeys(new Set(busyKeysRef.current));
    try {
      // Apply the response immediately, then refill from the server. The
      // refresh keeps loaded pages and supersedes older in-flight list reads.
      const result = await markInboxItem(activeScope, item, marks);
      notifyInboxChanged();
      if (request === requestRef.current) {
        setPage((current) => current && withItem(current, result.item, state));
        await refresh();
      }
    } catch (caught) {
      if (request === requestRef.current) setError(apiErrorMessage(caught));
    } finally {
      if (request === requestRef.current) {
        busyKeysRef.current.delete(key);
        setBusyKeys(new Set(busyKeysRef.current));
      }
    }
  }

  const rows = (items: InboxItem[]) => (
    <ul className="inbox-list">
      {items.map((item) => (
        <InboxItemRow
          busy={loadingMore || refreshing || busyKeys.has(itemKey(item))}
          item={item}
          key={itemKey(item)}
          onMark={(target, marks) => void mark(target, marks)}
        />
      ))}
    </ul>
  );
  const visibleItems = useMemo(() => {
    if (!page) return [];
    // Archived already contains every kind in the server's archive order.
    if (state === "archived") return page.items;
    return [...page.status_items, ...page.items].sort(newestFirst);
  }, [page, state]);
  const nothingLoaded = page !== null && page.items.length === 0 && page.status_items.length === 0;
  // A bounded scan can find nothing to show yet still leave older entries.
  const nothingShown = nothingLoaded && !page.next_cursor;
  const scanEmpty = nothingLoaded && Boolean(page.next_cursor);
  const groupTitle = (label: string, count: number) => (
    <h2 className="inbox-group-title">
      {label}
      <CountChip compact value={count} />
    </h2>
  );

  return (
    <div className="workspace-screen single-column">
      <section aria-busy={refreshing} className="feature-main inbox-screen">
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
          <SegmentedControl
            compact
            label={t("itemType.label")}
            onChange={setItemType}
            options={[
              { key: "all", label: t("itemType.all") },
              { key: "status", label: t("itemType.status") },
              { key: "message", label: t("itemType.message") },
            ]}
            value={itemType}
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
            {itemType === "status" && state !== "archived" ? (
              <p className="inbox-note">
                <Info size={14} aria-hidden="true" />
                {t("status.hint")}
              </p>
            ) : null}
            {nothingShown ? <p className="empty-state">{t(`empty.${state}`)}</p> : null}
            {visibleItems.length > 0 ? (
              <section className="feature-section inbox-group" aria-label={t("items.label")}>
                {groupTitle(t("items.label"), visibleItems.length)}
                {rows(visibleItems)}
              </section>
            ) : null}
            {scanEmpty ? <p className="inbox-scan-empty">{t("scanEmpty")}</p> : null}
            {page.next_cursor ? (
              <div className="load-more-row">
                <button
                  className="icon-button text-button"
                  disabled={loadingMore || refreshing || busyKeys.size > 0}
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
