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

/**
 * Multi-select workspace picker shared by every account-scoped MCP credential
 * surface (D-01v2: one credential holds a SET of allowed workspaces, not one
 * immutable binding).
 *
 * Deliberately presentational: the rows come from the caller, which reads
 * `useWorkspaceContext().workspaces` and refreshes it once when its dialog
 * opens. No component in `shared/components` opens its own workspace request,
 * and `SettingsScreen` renders in tests without a `WorkspaceProvider`, so a
 * context read inside here would either throw or silently show an empty list.
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
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const listRef = useRef<HTMLDivElement | null>(null);
  const selectAllRef = useRef<HTMLInputElement | null>(null);
  const selected = useMemo(() => new Set(selectedIds), [selectedIds]);
  const generatedId = useId();
  const normalizedQuery = normalize(query);

  // Section 3.2's order: selected first, then the default workspace, then
  // title. Inside a search it is the same rule over the matches.
  const shown = useMemo(() => {
    const filtered = workspaces.filter((option) =>
      matchesQuery(option, normalizedQuery),
    );
    return [...filtered].sort((left, right) => {
      const leftSelected = selected.has(left.id) ? 0 : 1;
      const rightSelected = selected.has(right.id) ? 0 : 1;
      if (leftSelected !== rightSelected) {
        return leftSelected - rightSelected;
      }
      const leftDefault = left.isDefault ? 0 : 1;
      const rightDefault = right.isDefault ? 0 : 1;
      if (leftDefault !== rightDefault) {
        return leftDefault - rightDefault;
      }
      return left.title.localeCompare(right.title);
    });
  }, [normalizedQuery, selected, workspaces]);

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

  const showSearch = workspaces.length > searchThreshold;
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
    const next = Math.min(
      Math.max(activeIndex + delta, 0),
      shown.length - 1,
    );
    setActiveIndex(next);
    const node = listRef.current?.querySelectorAll<HTMLElement>(
      '[role="option"]',
    )[next];
    node?.focus();
  }

  function onOptionKeyDown(
    event: ReactKeyboardEvent<HTMLDivElement>,
    option: WorkspaceScopeOption,
    index: number,
  ) {
    if (event.key === " " || event.key === "Spacebar" || event.key === "Enter") {
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
            onChange={() => (allShownSelected ? clearShown() : selectAllShown())}
            ref={selectAllRef}
            type="checkbox"
          />
          <span>{labels.selectAllShown}</span>
        </label>
        <button
          className="icon-button text-button workspace-scope-clear"
          disabled={disabled || shownSelectedCount === 0}
          onClick={clearShown}
          type="button"
        >
          {labels.clearAll}
        </button>
        <CountChip
          compact
          label={labels.selectedCount}
          outOf={workspaces.length}
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
            onChange={setQuery}
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
      <div
        aria-describedby={describedBy}
        aria-label={labels.listLabel}
        aria-multiselectable="true"
        className="workspace-scope-list"
        ref={listRef}
        role="listbox"
      >
        {shown.length === 0 ? (
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
      {selectedIds.length === 0 ? (
        <p className="workspace-scope-empty-warning" role="status">
          {labels.emptySelection}
        </p>
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
  maxVisible = 3,
  roleLabel,
  workspaces,
}: {
  labels: WorkspaceScopeChipsLabels;
  maxVisible?: number;
  roleLabel?: (role: string | null | undefined) => string | null;
  workspaces: WorkspaceGrantSummary[];
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    function onDocumentPointerDown(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocumentPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocumentPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

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
    <div className="workspace-chip-row" ref={containerRef}>
      {visible.map((workspace) => (
        <button
          className={`workspace-chip${
            workspace.syncState === "failed" ? " is-sync-failed" : ""
          }`}
          key={workspace.id}
          onClick={() => setOpen((current) => !current)}
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
          onClick={() => setOpen((current) => !current)}
          type="button"
        >
          {labels.more(overflow)}
        </button>
      ) : null}
      {open ? (
        <div
          aria-label={labels.popoverLabel}
          className="workspace-chip-popover"
          role="group"
        >
          <ul>
            {workspaces.map((workspace) => {
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
        </div>
      ) : null}
    </div>
  );
}
