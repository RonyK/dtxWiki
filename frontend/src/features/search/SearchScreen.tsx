import { ExternalLink, Search } from "lucide-react";
import type { FormEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import {
  getSearchCapabilities,
  searchWorkspace,
} from "../../shared/api/operations";
import type {
  RankedSearchResult,
  SearchPayload,
  SearchScope,
} from "../../shared/api/operations";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import { getCurrentWorkspaceId } from "../../shared/api/workspace";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import { SearchField } from "../../shared/components/SearchField";
import { Spinner } from "../../shared/components/Spinner";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { displayConceptPath } from "../../shared/lib/conceptPath";
import { domainLabel } from "../../shared/lib/domainLabel";
import { SEARCH_QUERY_MAX_LENGTH } from "../../shared/lib/searchLimits";
import { searchResultHref } from "../../shared/lib/searchResultHref";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";

const SCOPES: SearchScope[] = [
  "all",
  "context_page",
  "library_item",
  "library_artifact",
  "agent_page",
  "change",
];
const SEARCH_EXAMPLES = [
  { key: "embeddingPolicy", defaultValue: "embedding policy" },
  { key: "reviewGuideline", defaultValue: "review guideline" },
  { key: "sourceTrustLevel", defaultValue: "source trust level" },
];
type DisambiguationCandidate = {
  id?: string;
  concept_path?: string;
  title?: string;
};

class SearchIndexUnavailableError extends Error {
  constructor() {
    super();
    this.name = "SearchIndexUnavailableError";
  }
}

export function SearchScreen() {
  const { t } = useTranslation("search");
  // P10.1: ambient workspace id for canonical Context permalink hrefs on
  // search results and disambiguation candidates.
  const workspaceId = useOptionalWorkspaceContext()?.selectedId ?? null;
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<SearchScope>("all");
  const [retrievalMode, setRetrievalMode] = useState<string>(() =>
    parseMode(searchParams.get("mode")),
  );
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const indexUnavailableMessage = t("errors.indexUnavailable", {
    defaultValue:
      "Search index is not available in this build. Try Library, Context, or Agent navigation instead.",
  });
  const { data: capabilities } = useAsyncLoad(
    async () => {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        return { supported_retrieval_modes: ["keyword"] };
      }
      try {
        return await getSearchCapabilities(workspaceId);
      } catch {
        return { supported_retrieval_modes: ["keyword"] };
      }
    },
    [],
    {
      errorMessage: t("errors.capabilities", {
        defaultValue: "Failed to load search capabilities",
      }),
      fallbackData: null,
    },
  );
  const supportedModes = useMemo(
    () => capabilities?.supported_retrieval_modes ?? ["keyword"],
    [capabilities],
  );
  // Monotonically-increasing request id; each loader run captures its own id
  // and discards results when a newer request has started. This guards against
  // a race where an in-flight loader resolves *after* its AbortSignal fires
  // and before the new request settles.
  const requestIdRef = useRef(0);
  const {
    data: payload,
    error,
    loading,
    reload,
  } = useAsyncLoad<SearchPayload | null>(
    async (signal) => {
      const requestId = ++requestIdRef.current;
      if (!debouncedQuery || capabilities === null) {
        return null;
      }
      const workspaceId = await getCurrentWorkspaceId();
      // Bail out the moment a newer scope/query starts a fresh request or
      // the AbortController fires - either path means our result is stale
      // and must not be allowed to overwrite the in-flight response.
      if (signal?.aborted || requestId !== requestIdRef.current) {
        throw new DOMException("Aborted", "AbortError");
      }
      if (!workspaceId) {
        throw new Error("Workspace not found");
      }
      try {
        const result = await searchWorkspace(
          workspaceId,
          {
            query: debouncedQuery,
            scope,
            retrieval_mode: retrievalMode,
            limit: 20,
          },
          { signal },
        );
        if (signal?.aborted || requestId !== requestIdRef.current) {
          throw new DOMException("Aborted", "AbortError");
        }
        return result;
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === "AbortError") {
          throw caught;
        }
        if (caught instanceof ApiError && caught.status === 404) {
          throw new SearchIndexUnavailableError();
        }
        throw caught;
      }
    },
    [
      debouncedQuery,
      scope,
      retrievalMode,
      capabilities,
      indexUnavailableMessage,
    ],
    {
      errorMessage: t("errors.load", { defaultValue: "Failed to search" }),
      fallbackData: null,
      formatError: (caught, fallback) =>
        caught instanceof SearchIndexUnavailableError
          ? indexUnavailableMessage
          : apiErrorMessage(caught, fallback),
      signalAware: true,
    },
  );
  const searchLoading = loading && Boolean(debouncedQuery);

  // A5: search is keyset-paged. Accumulate appended pages on top of the loader's
  // first page, keyed by the active query+scope so a new search drops the stale
  // tail; the cursor guard mirrors the overview Load more.
  const searchBaseKey = `${debouncedQuery}:${scope}:${retrievalMode}`;
  const [appended, setAppended] = useState<{
    key: string;
    items: RankedSearchResult[];
    cursor: string | null;
  }>({ key: searchBaseKey, items: [], cursor: payload?.next_cursor ?? null });
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  // Reset the appended tail whenever a fresh first page lands. We key on the
  // `payload` object identity, not `next_cursor`: the keyset cursor is an opaque
  // offset token that is identical for page 1 of every query (the same
  // "offset=20"), so re-running the SAME query (submit/reload) returns a new
  // first page whose cursor is unchanged. Keying on next_cursor would skip the
  // reset and leave stale page-2+ rows mixed under a refreshed first page after
  // content/index changes.
  useEffect(() => {
    setAppended({
      key: searchBaseKey,
      items: [],
      cursor: payload?.next_cursor ?? null,
    });
    setLoadMoreError(null);
  }, [payload, searchBaseKey]);

  const appendedForBase = appended.key === searchBaseKey ? appended : null;
  const loadedResults = [
    ...(payload?.ranked_results ?? []),
    ...(appendedForBase?.items ?? []),
  ];
  const searchCursor = appendedForBase
    ? appendedForBase.cursor
    : payload?.next_cursor ?? null;
  const pagedPayload = payload
    ? { ...payload, ranked_results: loadedResults }
    : payload;

  async function loadMoreSearch() {
    if (!searchCursor || loadingMore || !debouncedQuery) {
      return;
    }
    const requestCursor = searchCursor;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const workspaceId = await getCurrentWorkspaceId();
      if (!workspaceId) {
        return;
      }
      const next = await searchWorkspace(workspaceId, {
        query: debouncedQuery,
        scope,
        retrieval_mode: retrievalMode,
        limit: 20,
        cursor: requestCursor,
      });
      setAppended((current) =>
        current.key === searchBaseKey && current.cursor === requestCursor
          ? {
              key: current.key,
              items: [...current.items, ...next.ranked_results],
              cursor: next.next_cursor,
            }
          : current,
      );
    } catch {
      // The cursor only advances on success, so the Load more button stays
      // available for retry; surface a localized error instead of a silent
      // unhandled rejection (mirrors the Agent overview Load more).
      setLoadMoreError(
        t("errors.loadMore", { defaultValue: "Failed to load more results" }),
      );
    } finally {
      setLoadingMore(false);
    }
  }

  useEffect(() => {
    const urlQuery =
      searchParams.get("q")?.trim().slice(0, SEARCH_QUERY_MAX_LENGTH) ?? "";
    const urlScope = parseScope(searchParams.get("scope"));
    setQuery(urlQuery);
    setScope(urlScope);
    setDebouncedQuery(urlQuery);
  }, [searchParams]);

  useEffect(() => {
    if (capabilities === null) return;
    const requestedMode = parseMode(searchParams.get("mode"));
    setRetrievalMode(
      supportedModes.includes(requestedMode) ? requestedMode : "keyword",
    );
  }, [capabilities, searchParams, supportedModes]);

  useEffect(() => {
    const trimmed = query.trim();
    if (!trimmed) {
      setDebouncedQuery("");
      return;
    }
    const handle = window.setTimeout(() => {
      setDebouncedQuery(trimmed);
    }, 300);
    return () => window.clearTimeout(handle);
  }, [query]);

  function updateQuery(value: string) {
    const nextValue = value.slice(0, SEARCH_QUERY_MAX_LENGTH);
    setQuery(nextValue);
    if (!nextValue.trim()) {
      setDebouncedQuery("");
      const nextParams = new URLSearchParams(searchParams);
      nextParams.delete("q");
      if (scope === "all") {
        nextParams.delete("scope");
      } else {
        nextParams.set("scope", scope);
      }
      if (retrievalMode === "keyword") nextParams.delete("mode");
      else nextParams.set("mode", retrievalMode);
      setSearchParams(nextParams, { replace: true });
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) {
      setDebouncedQuery("");
      const nextParams = new URLSearchParams(searchParams);
      nextParams.delete("q");
      if (scope === "all") {
        nextParams.delete("scope");
      } else {
        nextParams.set("scope", scope);
      }
      if (retrievalMode === "keyword") nextParams.delete("mode");
      else nextParams.set("mode", retrievalMode);
      setSearchParams(nextParams, { replace: true });
      return;
    }
    setSearchParams(searchParamsForQuery(trimmed, scope, retrievalMode));
    setDebouncedQuery(trimmed);
    reload();
  }

  function runSuggestedQuery(value: string) {
    setQuery(value);
    setSearchParams(searchParamsForQuery(value, scope, retrievalMode));
    setDebouncedQuery(value);
  }

  function updateScope(nextScope: SearchScope) {
    setScope(nextScope);
    const trimmed = query.trim();
    if (!trimmed || !debouncedQuery) {
      return;
    }
    // Keep the URL in step with the rescoped results so refresh/share
    // reproduces what is on screen, mirroring the q param handling.
    setSearchParams(searchParamsForQuery(trimmed, nextScope, retrievalMode), {
      replace: true,
    });
  }

  function updateRetrievalMode(nextMode: string) {
    setRetrievalMode(nextMode);
    const trimmed = query.trim();
    const next = trimmed
      ? searchParamsForQuery(trimmed, scope, nextMode)
      : new URLSearchParams(searchParams);
    if (!trimmed) {
      if (nextMode === "keyword") next.delete("mode");
      else next.set("mode", nextMode);
    }
    setSearchParams(next, { replace: true });
  }

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main">
        <Breadcrumbs items={[{ label: t("breadcrumb", { defaultValue: "Search" }) }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">
              {t("header.eyebrow", { defaultValue: "Workspace search" })}
            </p>
            <h1>{t("header.title", { defaultValue: "Search" })}</h1>
            <p className="feature-subtitle">
              {t("header.subtitle", {
                defaultValue:
                  "Search across Context pages, Library sources, Agent pages, and changes.",
              })}
            </p>
          </div>
        </div>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <form className="search-panel" onSubmit={submit}>
          <div className="search-panel-field">
            <label htmlFor="workspace-search-query">
              {t("form.query", { defaultValue: "Query" })}
            </label>
            <SearchField
              ariaLabel={t("form.query", { defaultValue: "Query" })}
              clearLabel={t("form.clear", { defaultValue: "Clear search" })}
              id="workspace-search-query"
              maxLength={SEARCH_QUERY_MAX_LENGTH}
              placeholder={t("form.queryPlaceholder", {
                defaultValue: "Search Context, Library, Agent pages, changes",
              })}
              value={query}
              onChange={updateQuery}
            />
          </div>
          <label>
            <span>{t("form.scope", { defaultValue: "Scope" })}</span>
            <select
              value={scope}
              onChange={(event) => updateScope(event.target.value as SearchScope)}
            >
              {SCOPES.map((entry) => (
                <option key={entry} value={entry}>
                  {t(`scopes.${entry}`, { defaultValue: defaultScopeLabel(entry) })}
                </option>
              ))}
            </select>
          </label>
          {supportedModes.length > 1 ? (
            <label>
              <span>{t("form.mode", { defaultValue: "Mode" })}</span>
              <select
                value={retrievalMode}
                onChange={(event) => updateRetrievalMode(event.target.value)}
              >
                {supportedModes.map((entry) => (
                  <option key={entry} value={entry}>
                    {t(`modes.${entry}`, { defaultValue: defaultModeLabel(entry) })}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <button
            aria-busy={searchLoading}
            className="icon-button primary"
            disabled={searchLoading || !query.trim()}
            type="submit"
          >
            <Search size={16} aria-hidden="true" />
            <span>{t("form.submit", { defaultValue: "Search" })}</span>
          </button>
        </form>
        <SearchResults
          loading={searchLoading}
          payload={pagedPayload}
          unavailable={Boolean(error && debouncedQuery)}
          onSelectSuggestion={runSuggestedQuery}
          workspaceId={workspaceId}
        />
        {loadMoreError ? (
          <StatusBanner tone="warning">{loadMoreError}</StatusBanner>
        ) : null}
        {searchCursor !== null && !error ? (
          <div className="load-more-row">
            <button
              className="icon-button text-button"
              disabled={loadingMore}
              type="button"
              onClick={() => void loadMoreSearch()}
            >
              {loadingMore ? t("loadingMore") : t("loadMore")}
            </button>
          </div>
        ) : null}
      </section>
    </div>
  );
}

function SearchResults({
  loading,
  payload,
  unavailable,
  onSelectSuggestion,
  workspaceId,
}: {
  loading: boolean;
  payload: SearchPayload | null;
  unavailable: boolean;
  onSelectSuggestion: (query: string) => void;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("search");
  const suggestions = SEARCH_EXAMPLES.map((entry) =>
    t(`examples.${entry.key}`, { defaultValue: entry.defaultValue }),
  );
  if (loading) {
    return (
      <div className="feature-section search-inline-loading">
        <Spinner aria-label={t("results.loading", { defaultValue: "Loading search results" })} />
        <span>{t("results.loadingLabel", { defaultValue: "Loading results" })}</span>
      </div>
    );
  }
  if (unavailable) {
    return (
      <div className="empty-state empty-state-rich">
        <h2>{t("empty.unavailableTitle", { defaultValue: "Search unavailable" })}</h2>
        <p>
          {t("empty.unavailableDescription", {
            defaultValue:
              "Use Library, Context, or Agent navigation while the search index is offline or not deployed in this build.",
          })}
        </p>
      </div>
    );
  }
  if (!payload) {
    return (
      <div className="empty-state empty-state-rich">
        <h2>{t("empty.initialTitle", { defaultValue: "Search your workspace" })}</h2>
        {/* Not a second statement of what is searchable — the header subtitle
            already says that, and this block sits in the same viewport. */}
        <p>
          {t("empty.initialDescription", {
            defaultValue:
              "Set a scope to narrow the search, or start from one of the examples below.",
          })}
        </p>
        <div className="empty-state-actions">
          <Link className="icon-button" to="/library">
            {t("empty.openLibrary", { defaultValue: "Open Library" })}
          </Link>
        </div>
        <div
          className="empty-suggestions"
          aria-label={t("examples.label", { defaultValue: "Search examples" })}
        >
          <span className="empty-suggestions-label">
            {t("examples.prompt", { defaultValue: "Try:" })}
          </span>
          {suggestions.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => onSelectSuggestion(suggestion)}
            >
              {suggestion}
            </button>
          ))}
        </div>
      </div>
    );
  }
  if (payload.ranked_results.length === 0) {
    return (
      <>
        <SearchAdvisories payload={payload} />
        <div className="empty-state empty-state-rich">
          <h2>{t("empty.noResultsTitle", { defaultValue: "No results" })}</h2>
          <p>
            {t("empty.noResultsDescription", {
              defaultValue:
                "Try a broader scope or search for a term from your Library or Context pages.",
            })}
          </p>
          <div className="empty-state-actions">
            <Link
              className="icon-button primary"
              to={newPageFromSearchHref(payload.raw_query)}
            >
              {t("empty.coverageGap", { defaultValue: "Create coverage" })}
            </Link>
            <Link className="icon-button" to="/library?from=search-zero">
              {t("empty.addLibrarySource", {
                defaultValue: "Add Library source",
              })}
            </Link>
            <button
              className="icon-button"
              type="button"
              onClick={() => onSelectSuggestion(suggestions[0])}
            >
              {t("empty.tryExample", { defaultValue: "Try an example" })}
            </button>
          </div>
        </div>
      </>
    );
  }
  return (
    <>
      <SearchAdvisories payload={payload} />
      <SearchDisambiguation payload={payload} workspaceId={workspaceId} />
      <section className="feature-section">
        <ListPanelHeader
          title={t("results.serverOrder", { defaultValue: "Results" })}
          count={payload.ranked_results.length}
          total={payload.total_count ?? payload.ranked_results.length}
          unit={t("units.results", {
            count: payload.total_count ?? payload.ranked_results.length,
            defaultValue_one: "result",
            defaultValue_other: "results",
          })}
        />
        <div className="operations-list">
          {payload.ranked_results.map((result) => (
            <SearchResultRow
              key={`${result.type}-${result.id}`}
              result={result}
              workspaceId={workspaceId}
            />
          ))}
        </div>
      </section>
    </>
  );
}

function SearchAdvisories({ payload }: { payload: SearchPayload }) {
  const { t } = useTranslation("search");
  const warnings = payload.warnings ?? [];
  // Registered advisory codes render as translated labels, never raw codes.
  const labels = warnings.map((code) =>
    t(`warningCodes.${code}`, { defaultValue: code }),
  );
  return (
    <>
      {labels.length > 0 ? (
        <StatusBanner tone="warning">
          {t("results.warningSummary", {
            defaultValue: labels.join(" · "),
            warnings: labels.join(" · "),
          })}
        </StatusBanner>
      ) : null}
    </>
  );
}

function SearchDisambiguation({
  payload,
  workspaceId,
}: {
  payload: SearchPayload;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("search");
  const disambiguation = payload.disambiguation;
  const candidates: DisambiguationCandidate[] =
    disambiguation &&
    typeof disambiguation === "object" &&
    "candidates" in disambiguation &&
    Array.isArray(disambiguation.candidates)
      ? (disambiguation.candidates as DisambiguationCandidate[])
      : [];
  if (candidates.length === 0) {
    return null;
  }
  return (
    <section className="feature-section search-disambiguation">
      <div className="panel-header">
        <span>
          {t("results.disambiguation", {
            defaultValue: "Multiple exact matches",
          })}
        </span>
      </div>
      <div className="search-disambiguation-list">
        {candidates.map((candidate) => {
          const href = disambiguationHref(candidate, workspaceId);
          const key = `${candidate.id}-${candidate.concept_path}`;
          const label =
            candidate.title ??
            (candidate.concept_path
              ? displayConceptPath(candidate.concept_path)
              : candidate.concept_path);
          // §12.2: candidate click = navigate; title alone cannot separate
          // same-named pages, so the concept path renders alongside.
          if (!href) {
            return (
              <span className="search-disambiguation-candidate" key={key}>
                <strong>{label}</strong>
                {candidate.concept_path ? (
                  <span>{displayConceptPath(candidate.concept_path)}</span>
                ) : null}
              </span>
            );
          }
          return (
            <Link
              className="search-disambiguation-candidate"
              key={key}
              to={href}
            >
              <strong>{label}</strong>
              {candidate.concept_path ? (
                <span>{candidate.concept_path}</span>
              ) : null}
            </Link>
          );
        })}
      </div>
    </section>
  );
}

function disambiguationHref(
  candidate: DisambiguationCandidate,
  workspaceId: string | null,
): string | null {
  if (!candidate.id) {
    return null;
  }
  // Disambiguation candidates come from the context-page entity lane; route
  // them through the shared href resolver so link semantics stay single-source.
  return searchResultHref(
    {
      id: candidate.id,
      type: "context_page",
      title: candidate.title ?? "",
      snippet: "",
      score: 0,
      citation_available: false,
      stale: false,
      archived: false,
      concept_path: candidate.concept_path,
    },
    workspaceId,
  );
}

function SearchResultRow({
  result,
  workspaceId,
}: {
  result: RankedSearchResult;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("search");
  const href = searchResultHref(result, workspaceId);
  const identity = identityForHref(href);
  const title = result.title;
  const snippet = result.snippet;
  const typeLabel = domainLabel("search", "types", result.type, t);
  const matchReason = result.match_reason
    ? t(`matchReasons.${result.match_reason}`, {
        defaultValue: defaultMatchReasonLabel(result.match_reason),
      })
    : null;
  const content = (
    <div className="search-result-body">
      {/* The result kind leads the row as a toned pill, the same slot Lint
          gives severity and Activity gives the actor. It used to be carried
          only by the trailing open-button's hue, while every row shared one
          identical leading glyph. */}
      <p className="search-result-title">
        <span
          className="search-result-kind"
          data-identity={identity ?? "muted"}
        >
          {typeLabel}
        </span>
        <span>{title}</span>
      </p>
      {snippet || result.matched_name ? (
        <p className="search-result-snippet">
          {snippet ||
            t("results.matchedName", {
              name: result.matched_name,
              defaultValue: `Matched name: ${result.matched_name}`,
            })}
        </p>
      ) : null}
      {result.concept_path ? (
        <p className="search-result-path">
          {displayConceptPath(result.concept_path)}
        </p>
      ) : null}
      <span className="row-badges search-result-chips">
        {/* Match reason and the open action are one bordered box split by a
            hairline -- the same joined segment as Lint's [rule | target].
            As two separate chips they never agreed: 20px against 24px. */}
        <span className="search-source-chip">
          {matchReason ? (
            <span className="search-source-chip__reason">{matchReason}</span>
          ) : null}
          <span
            className="search-source-chip__open"
            data-identity={identity ?? "muted"}
          >
            {href && identity ? (
              <>
                <ExternalLink size={13} aria-hidden="true" />
                {t(
                  `results.open${identity[0].toUpperCase()}${identity.slice(1)}`,
                  { defaultValue: typeLabel },
                )}
              </>
            ) : (
              t("results.openUnavailable", {
                defaultValue: "Open unavailable",
              })
            )}
          </span>
        </span>
        {result.stale ? <StatusPill status="stale" compact /> : null}
        {result.archived ? <StatusPill status="archived" compact /> : null}
        {!result.citation_available ? (
          <span className="search-result-meta">
            {t("results.noCitation", { defaultValue: "No citation" })}
          </span>
        ) : null}
      </span>
    </div>
  );
  if (!href) {
    return (
      <div
        aria-label={t("results.cannotOpen", {
          defaultValue: "{{title}} cannot be opened from search yet",
          title,
        })}
        className="search-result-row is-disabled"
      >
        {content}
      </div>
    );
  }
  return (
    <Link
      className="search-result-row linked"
      to={href}
      aria-label={t("results.open", {
        defaultValue: "Open {{title}}",
        title,
      })}
    >
      {content}
    </Link>
  );
}

function identityForHref(
  href: string | null,
): "context" | "library" | "agent" | "review" | null {
  if (!href) return null;
  if (href.startsWith("/context")) return "context";
  if (href.startsWith("/library")) return "library";
  if (href.startsWith("/agent")) return "agent";
  if (href.startsWith("/review")) return "review";
  return null;
}

function parseScope(value: string | null): SearchScope {
  return SCOPES.some((entry) => entry === value)
    ? (value as SearchScope)
    : "all";
}

function parseMode(value: string | null) {
  return value === "semantic" || value === "hybrid" ? value : "keyword";
}

function defaultScopeLabel(scope: SearchScope) {
  if (scope === "all") {
    return "All";
  }
  if (scope === "context_page") {
    return "Context";
  }
  if (scope === "library_item") {
    return "Library";
  }
  if (scope === "library_artifact") {
    return "Artifacts";
  }
  if (scope === "agent_page") {
    return "Agent";
  }
  return "Changes";
}

function defaultModeLabel(mode: string) {
  if (mode === "semantic") {
    return "Semantic retrieval";
  }
  if (mode === "hybrid") {
    return "Hybrid retrieval";
  }
  return "Keyword retrieval";
}

function searchParamsForQuery(
  query: string,
  scope: SearchScope,
  mode: string,
) {
  const params = new URLSearchParams({ q: query });
  if (scope !== "all") {
    params.set("scope", scope);
  }
  if (mode !== "keyword") params.set("mode", mode);
  return params;
}

function newPageFromSearchHref(query: string | undefined) {
  const params = new URLSearchParams({ from: "search-zero" });
  const title = (query ?? "").trim().slice(0, 160);
  if (title) params.set("title", title);
  return `/context/new?${params.toString()}`;
}

function defaultMatchReasonLabel(reason: string) {
  if (reason === "exact_id") return "Exact ID";
  if (reason === "exact_title") return "Exact title";
  if (reason === "exact_slug") return "Exact slug";
  if (reason === "exact_alias") return "Exact alias";
  if (reason === "trigram") return "Fuzzy name";
  if (reason === "grep_wiki") return "Grep";
  if (reason === "semantic") return "Semantic";
  if (reason === "hybrid") return "Hybrid";
  if (reason === "associative") return "Associated";
  return "Keyword";
}
