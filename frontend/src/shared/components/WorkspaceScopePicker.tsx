import { useTranslation } from "react-i18next";
import { Dialog, DialogCloseButton } from "./Dialog";
import { listCredentialWorkspaces } from "../api/credentialWorkspaces";

import { Check } from "lucide-react";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import { CountChip } from "./CountChip";
import { SearchField } from "./SearchField";
import { LoadingIndicator } from "./LoadingIndicator";

/**
 * Multi-select workspace picker shared by every account-scoped MCP credential
 * surface (D-01v2: one credential holds a SET of allowed workspaces, not one
 * immutable binding).
 *
 * The caller supplies the initial selected metadata, including remove-only
 * orphaned grants. Candidate pages come from membership-only server discovery
 * when serverPaging is enabled. Selected IDs and their metadata survive search
 * and cursor changes; late responses cannot overwrite the current query.
 */
export type WorkspaceScopeOption = {
  id: string;
  title: string;
  slug: string;
  role?: string | null;
  /** The user's pinned default workspace — sorts directly after the selection. */
  isDefault?: boolean;
  /**
   * Allowed by the credential, but the user has since lost membership. Such a
   * row stays visible (greyed) so the owner can see and remove it; it can be
   * unchecked but never re-checked.
   */
  membershipMissing?: boolean;
};

export type WorkspaceScopePickerLabels = {
  clearAll: string;
  defaultBadge: string;
  emptySelection: string;
  listLabel: string;
  membershipMissing: string;
  noMatches: string;
  searchAria: string;
  searchClear: string;
  searchPlaceholder: string;
  selectAllShown: string;
  selectedCount: string;
  toggleAllAria: string;
};

type WorkspaceScopePickerProps = {
  onOptionsLoaded?: (options: WorkspaceScopeOption[]) => void;
  serverPaging?: boolean;
  autoFocusSearch?: boolean;
  describedBy?: string;
  disabled?: boolean;
  labels: WorkspaceScopePickerLabels;
  onChange: (nextSelectedIds: string[]) => void;
  roleLabel?: (role: string | null | undefined) => string | null;
  /** Row count above which the local filter appears (shell switcher rule). */
  searchThreshold?: number;
  selectedIds: string[];
  workspaces: WorkspaceScopeOption[];
};

/** Server search waits for typing to pause; each request costs a count + page. */
const SERVER_SEARCH_DEBOUNCE_MS = 250;

function normalize(value: string) {
  return value.trim().toLowerCase();
}

function matchesQuery(option: WorkspaceScopeOption, query: string) {
  if (!query) {
    return true;
  }
  return (
    option.title.toLowerCase().includes(query) ||
    option.slug.toLowerCase().includes(query)
  );
}

export function WorkspaceScopePicker({
  onOptionsLoaded,
  serverPaging = false,
  autoFocusSearch = false,
  describedBy,
  disabled = false,
  labels,
  onChange,
  roleLabel,
  searchThreshold = 7,
  selectedIds,
  workspaces,
}: WorkspaceScopePickerProps) {
  const { t } = useTranslation("settings");
  const [query, setQuery] = useState("");
  // What the server was last asked for; trails `query` while typing.
  const [searchTerm, setSearchTerm] = useState("");
  const [page, setPage] = useState(0);
  const [availableCount, setAvailableCount] = useState(
    () => workspaces.filter((w) => !w.membershipMissing).length,
  );
  const [selectedOnly, setSelectedOnly] = useState(false);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [remote, setRemote] = useState<{
    rows: WorkspaceScopeOption[];
    total: number;
    next: string | null;
    key: string;
  } | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  const [known, setKnown] = useState<Map<string, WorkspaceScopeOption>>(
    () => new Map(workspaces.map((w) => [w.id, w])),
  );
  const loadedCallback = useRef(onOptionsLoaded);
  useEffect(() => {
    loadedCallback.current = onOptionsLoaded;
  }, [onOptionsLoaded]);
  const cursor = cursors[page] ?? null;
  const requestKey = JSON.stringify([searchTerm, cursor]);
  useEffect(() => {
    if (!serverPaging || selectedOnly || query === searchTerm) return;
    const timer = window.setTimeout(() => {
      setSearchTerm(query);
      setPage(0);
      setCursors([null]);
    }, SERVER_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [serverPaging, selectedOnly, query, searchTerm]);
  useEffect(() => {
    if (!serverPaging || selectedOnly) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(false);
    listCredentialWorkspaces(searchTerm, cursor)
      .then((result) => {
        if (cancelled) return;
        if (!searchTerm) setAvailableCount(result.total_count);
        const rows = result.data.map((w) => ({
          ...w,
          isDefault: w.is_default,
        }));
        loadedCallback.current?.(rows);
        setRemote({
          rows,
          total: result.total_count,
          next: result.next_cursor,
          key: JSON.stringify([searchTerm, cursor]),
        });
        setKnown(
          (current) =>
            new Map([...current, ...rows.map((w) => [w.id, w] as const)]),
        );
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [serverPaging, selectedOnly, searchTerm, cursor, retry]);
  function changeQuery(value: string) {
    setQuery(value);
    setActiveIndex(0);
    // Server paging resets when the debounced term lands; local lists now.
    if (!serverPaging || selectedOnly) {
      setPage(0);
      setCursors([null]);
    }
  }

  const [activeIndex, setActiveIndex] = useState(0);
  const listRef = useRef<HTMLDivElement | null>(null);
  const selectAllRef = useRef<HTMLInputElement | null>(null);
  const selected = useMemo(() => new Set(selectedIds), [selectedIds]);
  const allCount = serverPaging ? availableCount : workspaces.length;
  const generatedId = useId();
  const normalizedQuery = normalize(query);

  // Selection never affects ordering: checking a row cannot move it off-page.
  const filtered = useMemo(() => {
    const options = new Map([
      ...known,
      ...workspaces.map((w) => [w.id, w] as const),
    ]);
    return [...options.values()]
      .filter(
        (option) =>
          (!selectedOnly || selected.has(option.id)) &&
          matchesQuery(option, normalizedQuery),
      )
      .sort(
        (left, right) =>
          Number(Boolean(right.isDefault)) - Number(Boolean(left.isDefault)) ||
          left.title.localeCompare(right.title) ||
          left.id.localeCompare(right.id),
      );
  }, [known, workspaces, selectedOnly, selected, normalizedQuery]);
  const useRemote = serverPaging && !selectedOnly;
  // Keep the loading affordance visible from the first keystroke through the
  // debounce and request. Otherwise the empty-state message disappears before
  // the next request starts, leaving an unexplained blank list.
  const remoteLoading =
    useRemote && (loading || query !== searchTerm) && !loadError;
  const total = useRemote ? (remote?.total ?? 0) : filtered.length;
  const shown = useRemote
    ? loading || loadError || remote?.key !== requestKey
      ? []
      : remote.rows
    : filtered.slice(page * 8, (page + 1) * 8);
  const showNoMatches =
    shown.length === 0 &&
    (!useRemote ||
      (!loading && !loadError && remote?.key === requestKey && query === searchTerm));
  const hasNext = useRemote
    ? remote?.key === requestKey && Boolean(remote.next)
    : (page + 1) * 8 < total;

  useEffect(() => {
    setActiveIndex((current) =>
      shown.length === 0 ? 0 : Math.min(current, shown.length - 1),
    );
  }, [shown.length]);

  // A row the user has lost membership in can be removed but never re-added.
  const isLocked = useCallback(
    (option: WorkspaceScopeOption) =>
      Boolean(option.membershipMissing) && !selected.has(option.id),
    [selected],
  );

  const selectableShown = shown.filter((option) => !isLocked(option));
  const shownSelectedCount = shown.filter((option) =>
    selected.has(option.id),
  ).length;
  const allShownSelected =
    selectableShown.length > 0 && shownSelectedCount >= selectableShown.length;
  const someShownSelected = shownSelectedCount > 0 && !allShownSelected;

  useEffect(() => {
    if (selectAllRef.current) {
      // `indeterminate` is DOM-only state with no HTML attribute.
      selectAllRef.current.indeterminate = someShownSelected;
    }
  }, [someShownSelected]);

  const showSearch = serverPaging || workspaces.length > searchThreshold;
  const searchRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!autoFocusSearch || !showSearch) {
      return;
    }
    searchRef.current?.querySelector("input")?.focus();
  }, [autoFocusSearch, showSearch]);

  function toggle(option: WorkspaceScopeOption) {
    if (disabled || isLocked(option)) {
      return;
    }
    onChange(
      selected.has(option.id)
        ? selectedIds.filter((id) => id !== option.id)
        : [...selectedIds, option.id],
    );
  }

  function selectAllShown() {
    const next = new Set(selectedIds);
    for (const option of selectableShown) {
      next.add(option.id);
    }
    onChange(Array.from(next));
  }

  function clearShown() {
    const shownIds = new Set(shown.map((option) => option.id));
    onChange(selectedIds.filter((id) => !shownIds.has(id)));
  }

  function moveActive(delta: number) {
    if (shown.length === 0) {
      return;
    }
    const next = Math.min(Math.max(activeIndex + delta, 0), shown.length - 1);
    setActiveIndex(next);
    const node =
      listRef.current?.querySelectorAll<HTMLElement>('[role="option"]')[next];
    node?.focus();
  }

  function onOptionKeyDown(
    event: ReactKeyboardEvent<HTMLDivElement>,
    option: WorkspaceScopeOption,
    index: number,
  ) {
    if (
      event.key === " " ||
      event.key === "Spacebar" ||
      event.key === "Enter"
    ) {
      event.preventDefault();
      setActiveIndex(index);
      toggle(option);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      moveActive(1);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      moveActive(-1);
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      moveActive(-shown.length);
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      moveActive(shown.length);
    }
  }

  return (
    <div className="workspace-scope-picker">
      <div className="workspace-scope-toolbar">
        <label className="workspace-scope-select-all">
          <input
            aria-label={labels.toggleAllAria}
            checked={allShownSelected}
            disabled={disabled || selectableShown.length === 0}
            onChange={() =>
              allShownSelected ? clearShown() : selectAllShown()
            }
            ref={selectAllRef}
            type="checkbox"
          />
          <span>{labels.selectAllShown}</span>
        </label>
        <button
          className="icon-button text-button workspace-scope-clear"
          disabled={disabled || selectedIds.length === 0}
          onClick={() => onChange([])}
          type="button"
        >
          {labels.clearAll}
        </button>
        <CountChip
          compact
          label={labels.selectedCount}
          outOf={Math.max(allCount, selectedIds.length)}
          value={selectedIds.length}
        />
      </div>
      {showSearch ? (
        <div className="workspace-scope-search" ref={searchRef}>
          <SearchField
            ariaLabel={labels.searchAria}
            clearLabel={labels.searchClear}
            compact
            id={`${generatedId}-search`}
            onChange={changeQuery}
            onKeyDown={(event) => {
              // The create form wraps this picker; Enter in the filter must
              // not submit it (and issue a key) mid-narrowing.
              if (event.key === "Enter") {
                event.preventDefault();
              }
            }}
            placeholder={labels.searchPlaceholder}
            value={query}
          />
        </div>
      ) : null}
      <div className="workspace-scope-view-actions">
        <button
          type="button"
          className="icon-button text-button"
          aria-pressed={!selectedOnly}
          onClick={() => {
            setSelectedOnly(false);
            setPage(0);
            setCursors([null]);
            setActiveIndex(0);
          }}
        >
          {t("workspacePicker.showAll")} ({allCount})
        </button>
        <button
          type="button"
          className="icon-button text-button"
          aria-pressed={selectedOnly}
          onClick={() => {
            setSelectedOnly(true);
            setPage(0);
            setCursors([null]);
            setActiveIndex(0);
          }}
        >
          {t("workspacePicker.showSelected", { count: selectedIds.length })}
        </button>
        {remoteLoading ? (
          <LoadingIndicator as="p" className="workspace-scope-loading">
            {t("workspacePicker.loading")}
          </LoadingIndicator>
        ) : null}
        {useRemote && loadError ? (
          <div role="alert">
            {t("workspacePicker.loadError")}{" "}
            <button
              type="button"
              className="icon-button text-button"
              onClick={() => setRetry((n) => n + 1)}
            >
              {t("workspacePicker.retry")}
            </button>
          </div>
        ) : null}
      </div>
      <div
        aria-describedby={describedBy}
        aria-label={labels.listLabel}
        aria-multiselectable="true"
        className="workspace-scope-list"
        ref={listRef}
        role="listbox"
      >
        {showNoMatches ? (
          <p className="empty-state compact-empty">{labels.noMatches}</p>
        ) : (
          shown.map((option, index) => {
            const isSelected = selected.has(option.id);
            const locked = isLocked(option);
            const role = roleLabel?.(option.role) ?? null;
            return (
              <div
                aria-disabled={locked || disabled ? "true" : undefined}
                aria-selected={isSelected}
                className={`workspace-scope-option${
                  isSelected ? " is-selected" : ""
                }${option.membershipMissing ? " is-orphaned" : ""}`}
                key={option.id}
                onClick={() => {
                  setActiveIndex(index);
                  toggle(option);
                }}
                onKeyDown={(event) => onOptionKeyDown(event, option, index)}
                role="option"
                tabIndex={index === activeIndex ? 0 : -1}
              >
                <span aria-hidden="true" className="workspace-scope-check">
                  {isSelected ? <Check size={13} /> : null}
                </span>
                <span className="workspace-scope-copy">
                  <strong>{option.title}</strong>
                  <span className="meta-line">{option.slug}</span>
                </span>
                <span className="workspace-scope-badges">
                  {option.isDefault ? (
                    <span className="workspace-scope-badge">
                      {labels.defaultBadge}
                    </span>
                  ) : null}
                  {option.membershipMissing ? (
                    <span className="workspace-scope-badge is-orphaned">
                      {labels.membershipMissing}
                    </span>
                  ) : role ? (
                    <span className="workspace-scope-role">{role}</span>
                  ) : null}
                </span>
              </div>
            );
          })
        )}
      </div>
      <div className="workspace-scope-pagination">
        <span>
          {t("workspacePicker.range", {
            start: shown.length ? page * 8 + 1 : 0,
            end: shown.length ? Math.min((page + 1) * 8, total) : 0,
            total,
          })}
        </span>
        <button
          type="button"
          className="icon-button"
          disabled={(useRemote && loading) || page === 0}
          onClick={() => {
            setPage((n) => n - 1);
            setActiveIndex(0);
          }}
        >
          {t("workspacePicker.previous")}
        </button>
        <button
          type="button"
          className="icon-button"
          disabled={(useRemote && (loading || loadError)) || !hasNext}
          onClick={() => {
            if (useRemote && remote?.next)
              setCursors((current) => [
                ...current.slice(0, page + 1),
                remote.next,
              ]);
            setPage((n) => n + 1);
            setActiveIndex(0);
          }}
        >
          {t("workspacePicker.next")}
        </button>
      </div>
      {selectedIds.length === 0 ? (
        <p className="workspace-scope-empty-warning" role="status">
          {labels.emptySelection}
        </p>
      ) : null}
    </div>
  );
}

/** The create form commits a draft locally; only its submit issues a key. */
export function WorkspaceScopeSelector(props: WorkspaceScopePickerProps) {
  const { t } = useTranslation("settings");
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string[]>([]);
  const [names, setNames] = useState<Map<string, string>>(() => new Map());
  const titleId = useId();
  const summary = props.selectedIds
    .slice(0, 2)
    .map(
      (id) =>
        props.workspaces.find((w) => w.id === id)?.title ?? names.get(id) ?? id,
    )
    .join(", ");
  return (
    <div className="workspace-scope-summary">
      <span title={summary}>
        {t("workspacePicker.selectedCount", {
          count: props.selectedIds.length,
        })}
        {summary ? ` · ${summary}` : ""}
        {props.selectedIds.length > 2
          ? ` · ${t("workspacePicker.more", { count: props.selectedIds.length - 2 })}`
          : ""}
      </span>
      <button
        type="button"
        className="icon-button"
        disabled={props.disabled}
        onClick={() => {
          setDraft([...props.selectedIds]);
          setOpen(true);
        }}
      >
        {t("workspacePicker.open")}
      </button>
      {open ? (
        <Dialog
          className="workspace-picker-dialog"
          header={
            <div className="workspace-picker-heading">
              <h2 id={titleId}>{props.labels.listLabel}</h2>
              <DialogCloseButton
                label={t("workspacePicker.close")}
                onClick={() => setOpen(false)}
              />
            </div>
          }
          labelledBy={titleId}
          onClose={() => setOpen(false)}
          footer={
            <div className="modal-actions">
              <button
                type="button"
                className="icon-button"
                onClick={() => setOpen(false)}
              >
                {t("actions.cancel")}
              </button>
              <button
                type="button"
                className="icon-button primary"
                disabled={draft.length === 0}
                onClick={() => {
                  props.onChange(draft);
                  setOpen(false);
                }}
              >
                {t("workspacePicker.apply", { count: draft.length })}
              </button>
            </div>
          }
        >
          <WorkspaceScopePicker
            {...props}
            autoFocusSearch
            selectedIds={draft}
            onChange={setDraft}
            onOptionsLoaded={(options) =>
              setNames(
                (current) =>
                  new Map([
                    ...current,
                    ...options.map((w) => [w.id, w.title] as const),
                  ]),
              )
            }
          />
        </Dialog>
      ) : null}
    </div>
  );
}

export type WorkspaceGrantSummary = {
  id: string;
  title: string;
  slug: string;
  role?: string | null;
  scopes?: string[];
  /** Control -> workspace replica sync state: pending | synced | failed. */
  syncState?: string | null;
};

export type WorkspaceScopeChipsLabels = {
  more: (count: number) => string;
  none: string;
  popoverLabel: string;
  scopesLabel: string;
  showAll: string;
  syncFailed: string;
  syncFailedHint: string;
  syncPending: string;
};

/**
 * Read-only companion to the picker: the allowed set as it appears on a list
 * row. `ChipInput` is deliberately not reused — it is a free-text entry model,
 * and these chips are a projection of server rows with their own sync state.
 */
export function WorkspaceScopeChips({
  labels,
  maxVisible = 2,
  roleLabel,
  workspaces,
}: {
  labels: WorkspaceScopeChipsLabels;
  maxVisible?: number;
  roleLabel?: (role: string | null | undefined) => string | null;
  workspaces: WorkspaceGrantSummary[];
}) {
  const { t } = useTranslation("settings");
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const titleId = useId();
  const filtered = workspaces.filter((w) => matchesQuery(w, normalize(query)));
  const showAll = () => {
    setQuery("");
    setPage(0);
    setOpen(true);
  };

  if (workspaces.length === 0) {
    return (
      <div className="workspace-chip-row">
        <span className="workspace-chip is-empty">{labels.none}</span>
      </div>
    );
  }

  const visible = workspaces.slice(0, maxVisible);
  const overflow = workspaces.length - visible.length;

  return (
    <div className="workspace-chip-row">
      {visible.map((workspace) => (
        <button
          className={`workspace-chip${
            workspace.syncState === "failed" ? " is-sync-failed" : ""
          }`}
          key={workspace.id}
          onClick={showAll}
          title={
            workspace.syncState === "failed" ? labels.syncFailedHint : undefined
          }
          type="button"
        >
          <span className="workspace-chip-title">{workspace.title}</span>
          {workspace.syncState === "pending" ? (
            <span className="workspace-chip-sync">{labels.syncPending}</span>
          ) : null}
          {workspace.syncState === "failed" ? (
            <span className="workspace-chip-sync is-failed">
              {labels.syncFailed}
            </span>
          ) : null}
        </button>
      ))}
      {overflow > 0 ? (
        <button
          aria-label={labels.showAll}
          className="workspace-chip workspace-chip-more"
          onClick={showAll}
          type="button"
        >
          {labels.more(overflow)}
        </button>
      ) : null}
      {open ? (
        <Dialog
          className="workspace-picker-dialog"
          header={
            <div className="workspace-picker-heading">
              <h2 id={titleId}>{labels.popoverLabel}</h2>
              <DialogCloseButton
                label={t("workspacePicker.close")}
                onClick={() => setOpen(false)}
              />
            </div>
          }
          labelledBy={titleId}
          onClose={() => setOpen(false)}
          footer={
            <button
              type="button"
              className="icon-button"
              onClick={() => setOpen(false)}
            >
              {t("workspacePicker.close")}
            </button>
          }
        >
          <SearchField
            ariaLabel={t("workspacePicker.search")}
            clearLabel={t("workspacePicker.clearSearch")}
            id={`${titleId}-search`}
            value={query}
            onChange={(value) => {
              setQuery(value);
              setPage(0);
            }}
            placeholder={t("workspacePicker.search")}
          />
          <ul className="workspace-grant-details">
            {filtered.slice(page * 8, (page + 1) * 8).map((workspace) => {
              const role = roleLabel?.(workspace.role) ?? null;
              return (
                <li key={workspace.id}>
                  <strong>{workspace.title}</strong>
                  <span className="meta-line">
                    {[workspace.slug, role].filter(Boolean).join(" · ")}
                  </span>
                  <span className="meta-line">
                    {labels.scopesLabel}
                    {": "}
                    {workspace.scopes?.length
                      ? workspace.scopes.join(", ")
                      : "-"}
                  </span>
                  {workspace.syncState === "pending" ? (
                    <span className="workspace-chip-sync">
                      {labels.syncPending}
                    </span>
                  ) : null}
                  {workspace.syncState === "failed" ? (
                    <span className="workspace-chip-sync is-failed">
                      {labels.syncFailed} — {labels.syncFailedHint}
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
          {filtered.length === 0 ? (
            <p>{t("workspacePicker.noMatches")}</p>
          ) : null}
          <div className="workspace-scope-pagination">
            <span>
              {t("workspacePicker.range", {
                start: filtered.length ? page * 8 + 1 : 0,
                end: Math.min((page + 1) * 8, filtered.length),
                total: filtered.length,
              })}
            </span>
            <button
              type="button"
              className="icon-button"
              disabled={page === 0}
              onClick={() => setPage((n) => n - 1)}
            >
              {t("workspacePicker.previous")}
            </button>
            <button
              type="button"
              className="icon-button"
              disabled={(page + 1) * 8 >= filtered.length}
              onClick={() => setPage((n) => n + 1)}
            >
              {t("workspacePicker.next")}
            </button>
          </div>
        </Dialog>
      ) : null}
    </div>
  );
}
