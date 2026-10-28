import {
  Archive,
  ChevronDown,
  ChevronRight,
  FileText,
  MessagesSquare,
  RotateCcw,
} from "lucide-react";
import type { TFunction } from "i18next";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Link,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";

import {
  listMcpChats,
  type McpChatListData,
  type McpChatPeriod,
  type McpChatSessionRef,
  type McpChatSort,
} from "../../shared/api/operations";
import { getCurrentWorkspaceId } from "../../shared/api/workspace";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { RelativeTime } from "../../shared/components/RelativeTime";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { SearchField } from "../../shared/components/SearchField";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { useScreenEntryRefresh } from "../../shared/hooks/useScreenEntryRefresh";
import { useMediaQuery } from "../../shared/hooks/useMediaQuery";
import {
  formatGroupHeader,
  timeBucketFor,
  type TimeBucketKey,
} from "../../shared/lib/datetime";
import { groupLinkedArtifacts } from "./chatArtifacts";
import { artifactDisplayTitle, chatFailureCounts, chatSummaryLine } from "./chatLabels";
import { chatPresentationLine } from "./chatPresentation";
import { safeChatErrorMessage } from "./chatErrors";
import { ClientChip } from "./ClientChip";


const PERIODS: McpChatPeriod[] = ["today", "week", "month", "all"];
const SORTS: McpChatSort[] = ["last_activity_desc", "write_count_desc"];
// The backend scans at most 20 raw chats per continuation. Eight automatic
// hops cover 160 candidates while keeping one sparse search from creating an
// unbounded request loop; the visible Load more control can continue manually.
const SEARCH_AUTO_ADVANCE_MAX_HOPS = 8;

export function ChatsListScreen() {
  const { t, i18n } = useTranslation("chats");
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const filtersToggleRef = useRef<HTMLButtonElement | null>(null);
  const filtersPanelRef = useRef<HTMLDivElement | null>(null);
  const filtersPanelId = useId();
  const compactFilters = useMediaQuery("(max-width: 640px)");
  const [compactFiltersOpen, setCompactFiltersOpen] = useState(false);
  const filtersOpen = !compactFilters || compactFiltersOpen;
  const previousFiltersOpenRef = useRef(filtersOpen);
  const [archiveSuccess] = useState(() => readArchiveSuccess(location.state));
  const [archiveArrivalPath] = useState(
    () => `${location.pathname}${location.search}`,
  );
  const filters = readFilters(searchParams);
  const queryKey = [
    filters.q ?? "",
    filters.client ?? "",
    filters.period,
    String(filters.writes_only),
    String(filters.archived),
    filters.sort,
  ].join("\u001f");
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const [autoAdvancePaused, setAutoAdvancePaused] = useState(false);
  const paginationGenerationRef = useRef(0);
  const activeContinuationKeyRef = useRef("");
  const inFlightCursorRef = useRef<string | null>(null);
  const autoAdvanceRef = useRef({
    key: "",
    hops: 0,
    seen: new Set<string>(),
  });
  const { data, error, loading, reload, setData } = useAsyncLoad<{
    workspaceId: string;
    chats: McpChatListData;
  }>(
    async () => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        throw new Error(t("errors.workspaceNotFound"));
      }
      return {
        workspaceId,
        chats: await listMcpChats(workspaceId, filters),
      };
    },
    [
      filters.q,
      filters.client,
      filters.period,
      filters.writes_only,
      filters.archived,
      filters.sort,
    ],
    {
      errorMessage: t("errors.loadFailed"),
      onSuccess: () => {
        paginationGenerationRef.current += 1;
        inFlightCursorRef.current = null;
        autoAdvanceRef.current = { key: "", hops: 0, seen: new Set<string>() };
        setLoadingMore(false);
        setLoadMoreError(null);
        setAutoAdvancePaused(false);
      },
      formatError: (caught, fallback) =>
        safeChatErrorMessage(caught, fallback, [t("errors.workspaceNotFound")]),
    },
  );
  useScreenEntryRefresh("/chats", reload, {
    readArrivalNotice: readArchiveSuccess,
    queryDeps: [
      filters.q,
      filters.client,
      filters.period,
      filters.writes_only,
      filters.archived,
      filters.sort,
    ],
  });

  const continuationKey = `${queryKey}\u001e${data?.workspaceId ?? ""}`;
  const previousContinuationKeyRef = useRef(continuationKey);
  useLayoutEffect(() => {
    activeContinuationKeyRef.current = continuationKey;
    if (previousContinuationKeyRef.current === continuationKey) {
      return;
    }
    previousContinuationKeyRef.current = continuationKey;
    paginationGenerationRef.current += 1;
    inFlightCursorRef.current = null;
    autoAdvanceRef.current = {
      key: continuationKey,
      hops: 0,
      seen: new Set<string>(),
    };
    setAutoAdvancePaused(false);
    setLoadingMore(false);
    setLoadMoreError(null);
  }, [continuationKey]);

  function updateFilter(key: string, value: string | boolean | null) {
    // Codex #28: merge onto the LATEST URL via the functional updater so the
    // debounced `q` write does not clobber a period/sort/client filter the user
    // changed during the 300ms window (a captured snapshot would drop it).
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value === null || value === "" || value === false) {
          next.delete(key);
        } else {
          next.set(key, String(value));
        }
        return next;
      },
      { replace: true },
    );
  }

  // Local draft so the input stays responsive while the server fetch (keyed on
  // the URL `q` in the loader deps) is debounced — search-as-you-type no longer
  // fires one listMcpChats request per keystroke (C11).
  const [queryDraft, setQueryDraft] = useState(filters.q ?? "");
  // Only sync the draft from EXTERNAL URL changes (back/forward, reset). The
  // debounce below writes the TRIMMED draft, so syncing its own echo back into
  // the input would delete a trailing space mid-phrase and swallow keystrokes
  // typed between the URL write and this effect's commit.
  const lastCommittedQueryRef = useRef(filters.q ?? "");
  useEffect(() => {
    const committed = filters.q ?? "";
    if (committed === lastCommittedQueryRef.current) {
      return;
    }
    lastCommittedQueryRef.current = committed;
    setQueryDraft(committed);
  }, [filters.q]);
  useEffect(() => {
    const trimmed = queryDraft.trim();
    if (trimmed === (filters.q ?? "")) {
      return;
    }
    const handle = window.setTimeout(() => {
      lastCommittedQueryRef.current = trimmed;
      updateFilter("q", trimmed || null);
    }, 300);
    return () => window.clearTimeout(handle);
    // updateFilter closes over searchParams and is intentionally omitted; the
    // guard above prevents redundant scheduling once the URL catches up.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryDraft, filters.q]);

  const items = data?.chats.items ?? [];
  const clients = data?.chats.clients ?? [];
  const groupedItems = groupChatsByActivity(items, t, i18n.language);
  const hasActiveFilters = Boolean(
    filters.q ||
    filters.client ||
    filters.period !== "all" ||
    filters.writes_only ||
    filters.archived ||
    filters.sort !== "last_activity_desc",
  );
  const hasNonWritesFilters = Boolean(
    filters.q ||
    filters.client ||
    filters.period !== "all" ||
    filters.archived ||
    filters.sort !== "last_activity_desc",
  );
  const activeFilterLabels = [
    filters.q ? `${t("filters.find")}: ${filters.q}` : null,
    filters.client
      ? `${t("filters.client")}: ${
          clients.find((client) => client.id === filters.client)?.name ??
          t("filters.client")
        }`
      : null,
    filters.period !== "all"
      ? `${t("filters.period")}: ${t(
          `filters.periodOptions.${filters.period}`,
          { defaultValue: labelFor(filters.period) },
        )}`
      : null,
    filters.sort !== "last_activity_desc"
      ? `${t("filters.sort")}: ${t(`filters.sortOptions.${filters.sort}`, {
          defaultValue: labelFor(filters.sort),
        })}`
      : null,
    filters.writes_only ? t("filters.writesOnly") : null,
    filters.archived ? t("filters.showArchived") : null,
  ].filter((label): label is string => Boolean(label));
  const activeFilterSummary = activeFilterLabels.join(" · ");

  useLayoutEffect(() => {
    const wasOpen = previousFiltersOpenRef.current;
    previousFiltersOpenRef.current = filtersOpen;
    if (
      wasOpen &&
      !filtersOpen &&
      filtersPanelRef.current?.contains(document.activeElement)
    ) {
      filtersToggleRef.current?.focus({ preventScroll: true });
    }
  }, [filtersOpen]);

  useEffect(() => {
    const panel = filtersPanelRef.current;
    if (!compactFilters || !compactFiltersOpen || !panel) {
      return undefined;
    }
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      setCompactFiltersOpen(false);
      window.requestAnimationFrame(() => {
        filtersToggleRef.current?.focus({ preventScroll: true });
      });
    };
    panel.addEventListener("keydown", handleEscape);
    return () => panel.removeEventListener("keydown", handleEscape);
  }, [compactFilters, compactFiltersOpen]);

  const previousQueryKeyRef = useRef(queryKey);
  useEffect(() => {
    if (previousQueryKeyRef.current === queryKey) {
      return;
    }
    previousQueryKeyRef.current = queryKey;
    setData(null);
    setLoadMoreError(null);
  }, [queryKey, setData]);

  // Focus restoration is tied only to the explicit archive navigation state.
  // Ordinary list loads/filter refreshes remain passive and never steal focus.
  useEffect(() => {
    if (!archiveSuccess) {
      return;
    }
    headingRef.current?.focus({ preventScroll: true });
    headingRef.current?.scrollIntoView?.({ block: "nearest" });
    void navigate(archiveArrivalPath, {
      replace: true,
      state: null,
    });
  }, [archiveArrivalPath, archiveSuccess, navigate]);

  function resetFilters() {
    setSearchParams(new URLSearchParams(), { replace: true });
  }

  // Cursor-based "Load more" (server next_cursor), matching the Activity lane
  // pattern; without it sessions beyond the server page size are unreachable.
  const nextCursor = data?.chats.next_cursor ?? null;

  async function loadMore() {
    if (
      !data ||
      !nextCursor ||
      loadingMore ||
      inFlightCursorRef.current === nextCursor
    ) {
      return;
    }
    const requestGeneration = paginationGenerationRef.current;
    const requestContinuationKey = continuationKey;
    const requestWorkspaceId = data.workspaceId;
    inFlightCursorRef.current = nextCursor;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const nextPage = await listMcpChats(data.workspaceId, {
        ...filters,
        cursor: nextCursor,
      });
      setData((current) =>
        // Skip the append when a filter change replaced the base page while
        // this request was in flight. Cursor equality alone is insufficient:
        // two queries can legitimately receive the same typed scan cursor.
        current &&
        paginationGenerationRef.current === requestGeneration &&
        activeContinuationKeyRef.current === requestContinuationKey &&
        current.workspaceId === requestWorkspaceId &&
        current.chats.next_cursor === nextCursor
          ? {
              ...current,
              chats: {
                ...nextPage,
                items: [...current.chats.items, ...nextPage.items],
              },
            }
          : current,
      );
    } catch {
      if (
        paginationGenerationRef.current === requestGeneration &&
        activeContinuationKeyRef.current === requestContinuationKey
      ) {
        setLoadMoreError(t("errors.loadMoreFailed"));
        setAutoAdvancePaused(true);
      }
    } finally {
      if (
        paginationGenerationRef.current === requestGeneration &&
        activeContinuationKeyRef.current === requestContinuationKey &&
        inFlightCursorRef.current === nextCursor
      ) {
        inFlightCursorRef.current = null;
      }
      if (
        paginationGenerationRef.current === requestGeneration &&
        activeContinuationKeyRef.current === requestContinuationKey
      ) {
        setLoadingMore(false);
      }
    }
  }

  useEffect(() => {
    if (
      !filters.q ||
      !data ||
      data.chats.items.length > 0 ||
      !nextCursor ||
      loadingMore ||
      loadMoreError ||
      autoAdvancePaused
    ) {
      return;
    }
    const state = autoAdvanceRef.current;
    if (state.key !== continuationKey) {
      autoAdvanceRef.current = {
        key: continuationKey,
        hops: 0,
        seen: new Set<string>(),
      };
    }
    const current = autoAdvanceRef.current;
    if (
      current.hops >= SEARCH_AUTO_ADVANCE_MAX_HOPS ||
      current.seen.has(nextCursor)
    ) {
      setAutoAdvancePaused(true);
      return;
    }
    current.hops += 1;
    current.seen.add(nextCursor);
    void loadMore();
    // loadMore is intentionally guarded by the captured generation, workspace,
    // query key, and cursor. Re-running only follows a newly returned cursor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    autoAdvancePaused,
    continuationKey,
    data,
    filters.q,
    loadMoreError,
    loadingMore,
    nextCursor,
  ]);

  const searchContinuationPending = Boolean(
    filters.q && items.length === 0 && nextCursor,
  );

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main mcp-chats-screen">
        <Breadcrumbs items={[{ label: t("breadcrumb") }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{t("eyebrow")}</p>
            <h1 ref={headingRef} tabIndex={archiveSuccess ? -1 : undefined}>
              {t("title")}
            </h1>
            <p className="feature-subtitle">{t("description")}</p>
          </div>
          <CountChip
            value={items.length}
            label={countUnit(items.length, hasActiveFilters, t)}
            approxMore={Boolean(nextCursor)}
          />
        </div>
        <StatusBanner tone="info">{t("banner")}</StatusBanner>
        {archiveSuccess ? (
          <StatusBanner tone="info">{t("archiveSuccess")}</StatusBanner>
        ) : null}
        {error ? (
          <StatusBanner
            action={
              <button
                className="icon-button text-button"
                type="button"
                onClick={reload}
              >
                {t("retry")}
              </button>
            }
            tone="warning"
          >
            {error}
          </StatusBanner>
        ) : null}
        {loadMoreError ? (
          <StatusBanner
            action={
              <button
                className="icon-button text-button"
                type="button"
                onClick={() => void loadMore()}
              >
                {t("retry")}
              </button>
            }
            tone="warning"
          >
            {loadMoreError}
          </StatusBanner>
        ) : null}
        {loading && !data ? <RouteLoading label={t("loading")} /> : null}
        <div
          aria-label={t("filters.aria")}
          className="mcp-chat-filters mcp-chat-filter-disclosure"
          role="search"
        >
          {/* Row 1 — Find: search + client */}
          <div className="mcp-chat-filter-disclosure-head">
            <div className="mcp-chat-filter-stack mcp-chat-filter-search">
              <span className="eyebrow">{t("filters.find")}</span>
              <SearchField
                ariaLabel={t("filters.searchAria")}
                clearLabel={t("filters.searchClear")}
                compact
                placeholder={t("filters.searchPlaceholder")}
                value={queryDraft}
                onChange={(value) => setQueryDraft(value)}
              />
            </div>
            <div
              className={`mcp-chat-filter-disclosure-tools${
                hasActiveFilters ? " has-active-filters" : ""
              }`}
            >
              <button
                aria-controls={filtersPanelId}
                aria-expanded={filtersOpen}
                aria-label={t("filters.aria")}
                className="icon-button text-button mcp-chat-filter-disclosure-toggle"
                ref={filtersToggleRef}
                type="button"
                onClick={() => setCompactFiltersOpen((open) => !open)}
              >
                <span>{t("filters.aria")}</span>
                {activeFilterLabels.length > 0 ? (
                  <span
                    aria-hidden="true"
                    className="mcp-chat-filter-active-count"
                  >
                    {activeFilterLabels.length}
                  </span>
                ) : null}
                <ChevronDown size={16} aria-hidden="true" />
              </button>
              {hasActiveFilters ? (
                <span
                  aria-label={`${t("filters.aria")}: ${activeFilterSummary}`}
                  className="mcp-chat-filter-active-summary"
                  title={activeFilterSummary}
                >
                  {activeFilterSummary}
                </span>
              ) : null}
              {hasActiveFilters ? (
                <button
                  aria-label={t("filters.resetFilters")}
                  className="icon-button text-button mcp-chat-filters-reset"
                  type="button"
                  onClick={resetFilters}
                >
                  <RotateCcw size={15} aria-hidden="true" />
                  {t("filters.reset")}
                </button>
              ) : null}
            </div>
          </div>
          <div
            className="mcp-chat-filter-disclosure-panel"
            hidden={!filtersOpen}
            id={filtersPanelId}
            ref={filtersPanelRef}
          >
            <div className="mcp-chat-filters-row is-client">
              <div className="mcp-chat-filter-stack">
                {/* Every other filter is named by the eyebrow above it; the
                    client select follows the same rule instead of carrying its
                    own inline label. */}
                <span className="eyebrow">{t("filters.client")}</span>
                <label className="select-field compact">
                  <select
                    aria-label={t("filters.client")}
                    value={filters.client ?? ""}
                    onChange={(event) =>
                      updateFilter("client", event.currentTarget.value || null)
                    }
                  >
                    <option value="">{t("filters.allClients")}</option>
                    {clients.map((client) => (
                      <option key={client.id} value={client.id}>
                        {client.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </div>
            {/* Row 2 — View: period/sort + flags (period is the time filter) */}
            <div className="mcp-chat-filters-row is-view">
              <div className="mcp-chat-filters-group is-view">
                <div className="mcp-chat-filter-stack">
                  <span className="eyebrow">{t("filters.period")}</span>
                  <SegmentedControl
                    compact
                    label={t("filters.period")}
                    options={PERIODS.map((period) => ({
                      key: period,
                      label: t(`filters.periodOptions.${period}`, {
                        defaultValue: labelFor(period),
                      }),
                    }))}
                    value={filters.period ?? "all"}
                    onChange={(period) =>
                      updateFilter("period", period === "all" ? null : period)
                    }
                  />
                </div>
                <div className="mcp-chat-filter-stack">
                  <span className="eyebrow">{t("filters.sort")}</span>
                  <SegmentedControl
                    compact
                    label={t("filters.sort")}
                    options={SORTS.map((sort) => ({
                      key: sort,
                      label: t(`filters.sortOptions.${sort}`, {
                        defaultValue: labelFor(sort),
                      }),
                    }))}
                    value={filters.sort ?? "last_activity_desc"}
                    onChange={(sort) =>
                      updateFilter(
                        "sort",
                        sort === "last_activity_desc" ? null : sort,
                      )
                    }
                  />
                </div>
              </div>
              <div className="mcp-chat-filters-group is-flags">
                <span className="eyebrow">{t("filters.flags")}</span>
                <div className="mcp-chat-filter-controls">
                  <button
                    aria-pressed={filters.writes_only}
                    className={`filter-chip${filters.writes_only ? " active" : ""}`}
                    type="button"
                    onClick={() =>
                      updateFilter("writes_only", !filters.writes_only)
                    }
                  >
                    {t("filters.writesOnly")}
                  </button>
                  <button
                    aria-pressed={filters.archived}
                    className={`filter-chip${filters.archived ? " active" : ""}`}
                    type="button"
                    onClick={() => updateFilter("archived", !filters.archived)}
                  >
                    {t("filters.showArchived")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
        {!data ? null : (
          <section className="feature-section">
            {items.length === 0 ? (
              searchContinuationPending ? (
                autoAdvancePaused ? (
                  <div className="load-more-row">
                    <button
                      className="icon-button text-button"
                      disabled={loadingMore}
                      onClick={() => void loadMore()}
                      type="button"
                    >
                      {loadingMore ? t("loadingMore") : t("loadMore")}
                    </button>
                  </div>
                ) : (
                  <RouteLoading label={t("loadingMore")} />
                )
              ) : (
                <ChatsEmptyState
                  hasActiveFilters={hasActiveFilters}
                  writesOnly={filters.writes_only && !hasNonWritesFilters}
                  onReset={resetFilters}
                />
              )
            ) : (
              <>
                <div className="mcp-chat-groups">
                  {groupedItems.map((group) => (
                    <section className="mcp-chat-group" key={group.key}>
                      {groupedItems.length > 1 ? (
                        <div className="panel-header compact">
                          <span>{group.label}</span>
                          <CountChip
                            value={group.items.length}
                            label={t(
                              group.items.length === 1
                                ? "counts.session_one"
                                : "counts.session_other",
                            )}
                            compact
                          />
                        </div>
                      ) : null}
                      <div className="mcp-chat-card-list">
                        {group.entries.map((entry) =>
                          entry.conversationGroupId ? (
                            <ConversationGroup
                              chats={entry.items}
                              key={entry.key}
                            />
                          ) : (
                            <ChatCard key={entry.key} chat={entry.items[0]} />
                          ),
                        )}
                      </div>
                    </section>
                  ))}
                </div>
                {nextCursor ? (
                  <div className="load-more-row">
                    <button
                      className="icon-button text-button"
                      disabled={loadingMore}
                      onClick={() => void loadMore()}
                      type="button"
                    >
                      {loadingMore ? t("loadingMore") : t("loadMore")}
                    </button>
                  </div>
                ) : null}
              </>
            )}
          </section>
        )}
      </section>
    </div>
  );
}

function readArchiveSuccess(state: unknown): { chatId: string } | null {
  if (!state || typeof state !== "object") {
    return null;
  }
  const candidate = (state as { chatArchiveSuccess?: unknown })
    .chatArchiveSuccess;
  if (!candidate || typeof candidate !== "object") {
    return null;
  }
  const chatId = (candidate as { chatId?: unknown }).chatId;
  return typeof chatId === "string" && chatId ? { chatId } : null;
}

function ConversationGroup({ chats }: { chats: McpChatSessionRef[] }) {
  const { t, i18n } = useTranslation("chats");
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();

  // A one-session group is not a group in the list UI. Keeping the exact same
  // ChatCard path also keeps its content, link, and common-case interaction
  // identical to an ungrouped session.
  if (chats.length === 1) {
    return <ChatCard chat={chats[0]} />;
  }

  const newest = newestChat(chats);
  const writeCount = chats.reduce((total, chat) => total + chat.write_count, 0);
  const errorCount = chats.reduce((total, chat) => total + chat.error_count, 0);

  return (
    <section className="mcp-chat-conversation">
      <button
        aria-controls={contentId}
        aria-expanded={expanded}
        className="mcp-chat-card mcp-chat-conversation-toggle"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <MessagesSquare
          className="mcp-chat-conversation-icon"
          size={18}
          aria-hidden="true"
        />
        <div className="mcp-chat-card-body">
          <div className="mcp-chat-card-header">
            <div className="mcp-chat-card-title">
              <strong>{t("conversationGroup")}</strong>
              <span className="mcp-chat-card-subline">
                <span className="visually-hidden">
                  {t("conversationGroupNewestActivity")}:{" "}
                </span>
                <RelativeTime
                  className="mcp-chat-card-time"
                  value={newest.last_activity_at}
                  locale={i18n.language}
                />
              </span>
            </div>
            <ChevronRight
              className="mcp-chat-card-chevron"
              size={16}
              aria-hidden="true"
            />
          </div>
          <div className="mcp-chat-card-meta">
            <CountChip
              value={chats.length}
              label={t(
                chats.length === 1
                  ? "counts.session_one"
                  : "counts.session_other",
              )}
              compact
            />
            <CountChip
              value={writeCount}
              label={t(
                writeCount === 1 ? "counts.write_one" : "counts.write_other",
              )}
              tone="writes"
              compact
            />
            <CountChip
              value={errorCount}
              label={t(
                errorCount === 1 ? "counts.error_one" : "counts.error_other",
              )}
              tone={errorCount > 0 ? "danger" : "neutral"}
              compact
            />
          </div>
        </div>
      </button>
      <div
        className="mcp-chat-card-list mcp-chat-conversation-items"
        hidden={!expanded}
        id={contentId}
      >
        {chats.map((chat) => (
          <ChatCard key={chat.id} chat={chat} />
        ))}
      </div>
    </section>
  );
}

function ChatCard({ chat }: { chat: McpChatSessionRef }) {
  const { t, i18n } = useTranslation("chats");
  const artifactHint = linkedArtifactHint(chat.linked_artifacts, t);
  const summaryLine = chatSummaryLine(chat);
  const failures = chatFailureCounts(chat);
  const title = chatPresentationLine(chat, t);
  return (
    <Link className="mcp-chat-card" to={`/chats/${chat.id}`}>
      <ClientChip client={chat.client} />
      <div className="mcp-chat-card-body">
        <div className="mcp-chat-card-header">
          <div className="mcp-chat-card-title">
            <strong>{title}</strong>
            <span className="mcp-chat-card-subline">
              <RelativeTime
                className="mcp-chat-card-time"
                value={chat.last_activity_at}
                locale={i18n.language}
              />
            </span>
          </div>
          <ChevronRight
            className="mcp-chat-card-chevron"
            size={16}
            aria-hidden="true"
          />
        </div>
        {summaryLine ? (
          <p className="mcp-chat-card-summary">
            {summaryLine.source ? (
              <span className="mcp-chat-intent-source">
                {t(`intentSource.${summaryLine.source}`)}
              </span>
            ) : null}
            {summaryLine.text}
          </p>
        ) : null}
        <div className="mcp-chat-card-meta">
          <CountChip
            value={chat.tool_call_count}
            label={t(
              chat.tool_call_count === 1
                ? "counts.call_one"
                : "counts.call_other",
            )}
            tone="calls"
            compact
          />
          <CountChip
            value={chat.write_count}
            label={t(
              chat.write_count === 1
                ? "counts.write_one"
                : "counts.write_other",
            )}
            tone="writes"
            compact
          />
          {failures && "combined" in failures ? (
            <CountChip
              value={failures.combined}
              label={t(
                failures.combined === 1
                  ? "counts.error_one"
                  : "counts.error_other",
              )}
              tone="danger"
              compact
            />
          ) : null}
          {failures && "failed" in failures && failures.failed > 0 ? (
            <CountChip
              value={failures.failed}
              label={t(
                failures.failed === 1
                  ? "counts.failed_one"
                  : "counts.failed_other",
              )}
              tone="danger"
              compact
            />
          ) : null}
          {failures && "rejected" in failures && failures.rejected > 0 ? (
            <CountChip
              value={failures.rejected}
              label={t(
                failures.rejected === 1
                  ? "counts.rejected_one"
                  : "counts.rejected_other",
              )}
              tone="neutral"
              compact
            />
          ) : null}
          {artifactHint && !summaryLine?.artifactFallback ? (
            <span className="mcp-chat-card-artifacts" title={artifactHint.full}>
              <FileText size={13} aria-hidden="true" />
              <span className="artifact-label">{artifactHint.title}</span>
              {artifactHint.more ? (
                <>
                  {" "}
                  <span className="artifact-more">{t("card.linkedArtifactsMore")}</span>
                </>
              ) : null}
            </span>
          ) : null}
          {chat.archived_at ? (
            <Archive
              className="mcp-chat-card-archived"
              size={15}
              aria-label={t("card.archived")}
            />
          ) : null}
        </div>
      </div>
    </Link>
  );
}

function linkedArtifactHint(
  artifacts: McpChatSessionRef["linked_artifacts"],
  t: TFunction<"chats">,
) {
  if (!artifacts || artifacts.length === 0) {
    return null;
  }
  // The list response carries only a chat's few most recent non-search rows
  // (link_limit in mcp_chat_service.py), so a count here would pass a sample
  // off as a total. Name the newest artifact, mark that there are others, and
  // offer the sampled titles as "recently linked".
  const titles = groupLinkedArtifacts(artifacts).map(({ latest }) =>
    artifactDisplayTitle(latest, t),
  );
  const more = titles.length > 1;
  return {
    title: titles[0],
    more,
    full: more ? t("card.recentlyLinked", { titles: titles.join(", ") }) : titles[0],
  };
}

function ChatsEmptyState({
  writesOnly,
  hasActiveFilters,
  onReset,
}: {
  writesOnly: boolean;
  hasActiveFilters: boolean;
  onReset: () => void;
}) {
  const { t } = useTranslation("chats");
  if (writesOnly) {
    return (
      <div className="empty-state empty-state-rich">
        <MessagesSquare size={22} aria-hidden="true" />
        <h2>{t("empty.writesOnlyTitle")}</h2>
        <p>{t("empty.writesOnlyBody")}</p>
        <div className="empty-state-actions">
          <button
            className="icon-button text-button"
            type="button"
            onClick={onReset}
          >
            <RotateCcw size={15} aria-hidden="true" />
            {t("filters.resetFilters")}
          </button>
        </div>
      </div>
    );
  }
  if (hasActiveFilters) {
    return (
      <div className="empty-state empty-state-rich">
        <MessagesSquare size={22} aria-hidden="true" />
        <h2>{t("empty.filteredTitle")}</h2>
        <p>{t("empty.filteredBody")}</p>
        <div className="empty-state-actions">
          <button
            className="icon-button text-button"
            type="button"
            onClick={onReset}
          >
            <RotateCcw size={15} aria-hidden="true" />
            {t("filters.resetFilters")}
          </button>
        </div>
      </div>
    );
  }
  return (
    <div className="empty-state empty-state-rich">
      <MessagesSquare size={22} aria-hidden="true" />
      <h2>{t("empty.zeroTitle")}</h2>
      <p>{t("empty.zeroBody")}</p>
      <div className="empty-state-actions">
        <Link className="icon-button text-button" to="/settings/integrations">
          {t("empty.openMcpSetup")}
        </Link>
      </div>
    </div>
  );
}


function readFilters(searchParams: URLSearchParams) {
  return {
    q: searchParams.get("q") || undefined,
    client: searchParams.get("client") || undefined,
    period: parsePeriod(searchParams.get("period")),
    writes_only: searchParams.get("writes_only") === "true",
    archived: searchParams.get("archived") === "true",
    sort: parseSort(searchParams.get("sort")),
  };
}

function parsePeriod(value: string | null): McpChatPeriod {
  return PERIODS.includes(value as McpChatPeriod)
    ? (value as McpChatPeriod)
    : "all";
}

function parseSort(value: string | null): McpChatSort {
  return value === "write_count_desc"
    ? "write_count_desc"
    : "last_activity_desc";
}

function countUnit(count: number, filtered: boolean, t: TFunction<"chats">) {
  if (filtered) {
    return t(count === 1 ? "counts.match_one" : "counts.match_other");
  }
  return t(count === 1 ? "counts.session_one" : "counts.session_other");
}

function labelFor(value: string) {
  return value.replace(/_/g, " ");
}

const TIME_BUCKET_ORDER: readonly TimeBucketKey[] = [
  "today",
  "yesterday",
  "last_7_days",
  "last_30_days",
  "older",
];

function groupChatsByActivity(
  items: McpChatSessionRef[],
  t: TFunction<"chats">,
  locale: string,
) {
  const entries = conversationEntries(items);

  const groups = new Map<TimeBucketKey, typeof entries>();
  entries.forEach((entry) => {
    const key = timeBucketFor(entry.newestActivityAt);
    const bucket = groups.get(key) ?? [];
    bucket.push(entry);
    groups.set(key, bucket);
  });
  // Render buckets in fixed chronological order. Map preserves first-insertion
  // order, which under Sort="Most writes" follows the write-count ordering and
  // would scramble the time-bucket headers (e.g. "Older" above "Today") (C12).
  return Array.from(groups.entries())
    .map(([key, groupEntries]) => ({
      key,
      label: formatGroupHeader(key, t, locale),
      entries: groupEntries,
      items: groupEntries.flatMap((entry) => entry.items),
    }))
    .sort(
      (a, b) =>
        TIME_BUCKET_ORDER.indexOf(a.key) - TIME_BUCKET_ORDER.indexOf(b.key),
    );
}

type ChatListEntry = {
  key: string;
  conversationGroupId: string | null;
  items: McpChatSessionRef[];
  newestActivityAt: string;
};

function conversationEntries(items: McpChatSessionRef[]): ChatListEntry[] {
  const grouped = new Map<
    string,
    {
      items: McpChatSessionRef[];
      newest: McpChatSessionRef;
      newestIndex: number;
    }
  >();

  items.forEach((item, index) => {
    const groupId = item.conversation_group_id;
    if (!groupId) {
      return;
    }
    const group = grouped.get(groupId);
    if (!group) {
      grouped.set(groupId, {
        items: [item],
        newest: item,
        newestIndex: index,
      });
      return;
    }
    group.items.push(item);
    if (
      new Date(item.last_activity_at).getTime() >
      new Date(group.newest.last_activity_at).getTime()
    ) {
      group.newest = item;
      group.newestIndex = index;
    }
  });

  // Iterate the already-sorted payload once and emit a group at the position
  // occupied by its newest member. This keeps the active server sort as the
  // source of list order and never sorts the group's member array.
  const entries: ChatListEntry[] = [];
  items.forEach((item, index) => {
    const groupId = item.conversation_group_id;
    if (!groupId) {
      entries.push({
        key: `chat:${item.id}`,
        conversationGroupId: null,
        items: [item],
        newestActivityAt: item.last_activity_at,
      });
      return;
    }
    const group = grouped.get(groupId);
    if (!group || group.newestIndex !== index) {
      return;
    }
    entries.push({
      key: `conversation:${groupId}`,
      conversationGroupId: groupId,
      items: group.items,
      newestActivityAt: group.newest.last_activity_at,
    });
  });
  return entries;
}

function newestChat(chats: McpChatSessionRef[]): McpChatSessionRef {
  return chats.reduce((newest, chat) =>
    new Date(chat.last_activity_at).getTime() >
    new Date(newest.last_activity_at).getTime()
      ? chat
      : newest,
  );
}
