import type { ReactNode } from "react";
import { useEffect, useRef } from "react";

import { apiErrorMessage } from "../api/client";
import { LoadingIndicator } from "./LoadingIndicator";
import { SearchField } from "./SearchField";
import { SegmentedControl } from "./SegmentedControl";
import { StatusBanner } from "./StatusBanner";

export type CredentialFilterOption<TFilter extends string> = {
  count: number;
  key: TFilter;
  label: string;
};

type PaginatedCredentialListProps<TFilter extends string> = {
  children: ReactNode;
  emptyText: string;
  error: unknown | null;
  errorMessage: string;
  filter: TFilter;
  filterLabel: string;
  filterOptions: Array<CredentialFilterOption<TFilter>>;
  filteredCount: number;
  focusRevision: number;
  hasNext: boolean;
  hasPrevious: boolean;
  itemCount: number;
  loading: boolean;
  loadingText: string;
  nextLabel: string;
  onFilterChange: (filter: TFilter) => void;
  onNext: () => void;
  onPrevious: () => void;
  onRetry?: () => void;
  onSearchChange?: (value: string) => void;
  onSearchSubmit?: (value: string) => void;
  pageIndex: number;
  pageSize: number;
  previousLabel: string;
  rangeLabel: (input: { end: number; start: number; total: number }) => string;
  recoveredFromCursor: boolean;
  recoveredText: string;
  retryLabel?: string;
  searchClearLabel?: string;
  searchLabel?: string;
  searchPlaceholder?: string;
  searchSubmitLabel?: string;
  searchValue?: string;
};

/** Shared list chrome only; lifecycle vocabulary and row actions are adapters. */
export function PaginatedCredentialList<TFilter extends string>({
  children,
  emptyText,
  error,
  errorMessage,
  filter,
  filterLabel,
  filterOptions,
  filteredCount,
  focusRevision,
  hasNext,
  hasPrevious,
  itemCount,
  loading,
  loadingText,
  nextLabel,
  onFilterChange,
  onNext,
  onPrevious,
  onRetry,
  onSearchChange,
  onSearchSubmit,
  pageIndex,
  pageSize,
  previousLabel,
  rangeLabel,
  recoveredFromCursor,
  recoveredText,
  retryLabel,
  searchClearLabel,
  searchLabel,
  searchPlaceholder,
  searchSubmitLabel,
  searchValue,
}: PaginatedCredentialListProps<TFilter>) {
  const rangeRef = useRef<HTMLParagraphElement | null>(null);
  const previousFocusRevision = useRef(focusRevision);
  const start = itemCount === 0 ? 0 : pageIndex * pageSize + 1;
  const end = itemCount === 0 ? 0 : start + itemCount - 1;

  useEffect(() => {
    if (focusRevision === previousFocusRevision.current) {
      return;
    }
    previousFocusRevision.current = focusRevision;
    rangeRef.current?.focus();
  }, [focusRevision]);

  return (
    <div className="paginated-credential-list">
      <div className="credential-list-controls">
        <SegmentedControl
          compact
          label={filterLabel}
          onChange={onFilterChange}
          options={filterOptions.map((option) => ({
            key: option.key,
            label: `${option.label} ${option.count}`,
          }))}
          value={filter}
        />
        {searchLabel && onSearchChange && onSearchSubmit ? (
          <form
            className="credential-list-search"
            onSubmit={(event) => {
              event.preventDefault();
              onSearchSubmit(searchValue ?? "");
            }}
            role="search"
          >
            <SearchField
              ariaLabel={searchLabel}
              clearLabel={searchClearLabel ?? searchLabel}
              compact
              loading={loading}
              onChange={onSearchChange}
              onClear={() => onSearchSubmit("")}
              placeholder={searchPlaceholder}
              submitLabel={searchSubmitLabel}
              value={searchValue ?? ""}
            />
          </form>
        ) : null}
      </div>
      {recoveredFromCursor ? (
        <StatusBanner tone="info">{recoveredText}</StatusBanner>
      ) : null}
      {error ? (
        <div className="credential-list-recovery">
          <StatusBanner tone="warning">
            {apiErrorMessage(error, errorMessage)}
          </StatusBanner>
          {onRetry && retryLabel ? (
            <button
              className="icon-button text-button"
              disabled={loading}
              onClick={onRetry}
              type="button"
            >
              {retryLabel}
            </button>
          ) : null}
        </div>
      ) : null}
      {loading ? <LoadingIndicator>{loadingText}</LoadingIndicator> : null}
      {!loading && !error && itemCount === 0 ? (
        <div className="empty-state compact-empty">{emptyText}</div>
      ) : null}
      {/* Keep the loading announcement outside the busy results subtree. */}
      {itemCount > 0 ? (
        <div aria-busy={loading} className="credential-list-results">
          {children}
        </div>
      ) : null}
      {itemCount > 0 || filteredCount > 0 ? (
        <div className="credential-list-pagination">
          <p
            aria-live="polite"
            className="credential-list-range"
            ref={rangeRef}
            tabIndex={-1}
          >
            {rangeLabel({ end, start, total: filteredCount })}
          </p>
          <div className="credential-list-page-actions">
            <button
              className="icon-button text-button"
              disabled={!hasPrevious || loading}
              onClick={onPrevious}
              type="button"
            >
              {previousLabel}
            </button>
            <button
              className="icon-button text-button"
              disabled={!hasNext || loading}
              onClick={onNext}
              type="button"
            >
              {nextLabel}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
