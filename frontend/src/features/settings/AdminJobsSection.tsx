import { RefreshCw, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  fetchAdminJobQueueSummary,
  listAdminJobs,
  retryAdminJob,
  type AdminJobListData,
  type AdminJobQueueSummary,
  type Job,
} from "../../shared/api/adminJobs";
import { listWorkspaceJobs } from "../../shared/api/operations";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import {
  SettingsLoadingRow,
  SettingsPaneHeader,
} from "./SettingsPaneParts";
import { formatVersionTimestamp } from "../../shared/lib/datetime";

// Epic-2 Phase 1c (D-6): the operator screen for background jobs. Job already
// records the structured state rendered here (status/stage/progress/retry_count/
// error_code). Raw worker exceptions remain in protected operator diagnostics --
// this is only the read+retry surface that was missing. Deliberately reuses
// the AdminUserRecoverySection pattern: a self-contained section mounted into
// the existing Settings shell, gated by isServerAdmin in SettingsScreen, no
// new route.
// `scope` picks which of the two job surfaces backs this screen. They render
// the same way on purpose: someone reading a stuck queue wants the same facts
// whether they administer the server or only this workspace.
//
// - "server": the cross-workspace admin endpoints, plus retry. Retry stays
//   server-admin-only because it can requeue any workspace's job.
// - "workspace": GET /workspaces/{id}/jobs, readable by any workspace viewer.
//   No retry control -- there is no workspace-scoped retry endpoint, and a
//   button that 403s is worse than no button.
export type AdminJobsScope = "server" | "workspace";

export function AdminJobsSection({
  scope = "server",
  workspaceId = null,
}: {
  scope?: AdminJobsScope;
  // Which workspace the "workspace" scope reads. Passed in rather than
  // resolved from the module-global current workspace, because this component
  // outlives a workspace change: SettingsScreen keeps it mounted, so reading
  // the global inside load() meant the effect never learned the workspace had
  // moved and one workspace's jobs stayed on screen under another's name.
  // Ignored under "server" scope, which is cross-workspace by definition.
  workspaceId?: string | null;
}) {
  const { t } = useTranslation(["settings", "activity"]);
  const serverScope = scope === "server";
  const [summary, setSummary] = useState<AdminJobQueueSummary | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [problemsOnly, setProblemsOnly] = useState(true);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [retryError, setRetryError] = useState<string | null>(null);
  // The server-admin summary/list endpoints fan out across every workspace's
  // Job table and skip a shard that cannot be reached rather than 500ing.
  // A full load() replaces this with the fresh snapshot from that reload;
  // loadMore() unions into it, because a later page can hit a shard an
  // earlier page did not -- the banner should not drop while pages already
  // shown are still missing a shard's jobs.
  const [unreachableWorkspaceIds, setUnreachableWorkspaceIds] = useState<
    Set<string>
  >(new Set());
  const problemsOnlyRef = useRef(problemsOnly);
  const loadGenerationRef = useRef(0);
  const retryingIdRef = useRef<string | null>(null);

  const load = useCallback(
    async (nextProblemsOnly: boolean) => {
      const generation = loadGenerationRef.current + 1;
      loadGenerationRef.current = generation;
      setLoading(true);
      setLoadingMore(false);
      setError(null);
      setRetryError(null);
      try {
        // The workspace surface has no summary endpoint: queue-depth counts
        // spanning every workspace are a server-admin concept, so it shows the
        // list alone rather than a number that would quietly mean something
        // different from the one an admin reads.
        const [summaryResult, jobsResult, unreachableIds] = serverScope
          ? await (async () => {
              const [summaryData, jobsData] = await Promise.all([
                fetchAdminJobQueueSummary(),
                listAdminJobs({ problems_only: nextProblemsOnly, limit: 50 }),
              ]);
              return [
                summaryData,
                jobsData,
                [
                  ...summaryData.unreachable_workspace_ids,
                  ...jobsData.unreachable_workspace_ids,
                ],
              ] as const;
            })()
          : await (async () => {
              // The prop is authoritative when the host knows the selection;
              // the global stays the fallback for a host with no workspace
              // context at all, where there is no selection to go stale.
              const resolvedWorkspaceId =
                workspaceId;
              if (!resolvedWorkspaceId) {
                throw new Error("no current workspace");
              }
              return [
                null,
                await listWorkspaceJobs(resolvedWorkspaceId, {
                  problems_only: nextProblemsOnly,
                  limit: 50,
                }),
                [],
              ] as const;
            })();
        if (generation !== loadGenerationRef.current) {
          return;
        }
        setSummary(summaryResult);
        setJobs(jobsResult.items);
        setNextCursor(jobsResult.next_cursor);
        // Fresh snapshot from this reload -- replace, not union (see the
        // state's own comment for why loadMore below unions instead).
        setUnreachableWorkspaceIds(new Set(unreachableIds));
      } catch (caught) {
        if (generation !== loadGenerationRef.current) {
          return;
        }
        setError(
          caught instanceof ApiError
            ? apiErrorMessage(
                caught,
                t("jobs.loadError", { defaultValue: "Could not load jobs." }),
              )
            : t("jobs.loadError", { defaultValue: "Could not load jobs." }),
        );
      } finally {
        if (generation === loadGenerationRef.current) {
          setLoading(false);
        }
      }
    },
    // `serverScope` decides WHICH endpoint this calls, so it belongs here.
    // It is not a constant: SettingsScreen derives the `scope` prop from
    // isServerAdmin, which starts false and flips only once /auth/me
    // resolves. Memoizing on `t` alone froze the first (workspace) value
    // into the callback, so an admin landing straight on the jobs section
    // kept workspace-scoped results under a server-admin UI -- and Refresh
    // re-invoked that same stale callback forever.
    [serverScope, workspaceId, t],
  );

  const previousWorkspaceIdRef = useRef(workspaceId);
  useEffect(() => {
    // Reset the state the `loading` gate does NOT hide. The row list and the
    // summary are behind `loading`, and load() already clears both error
    // fields, so those need nothing here. `nextCursor` is different: the
    // "Load more" control renders on `nextCursor` alone, so the previous
    // workspace's cursor stays on screen for as long as the new workspace
    // takes to answer -- and a cursor is workspace-specific. Same shape as
    // McpAppHub's previousContextKeyRef reset, scoped to what actually leaks.
    if (previousWorkspaceIdRef.current !== workspaceId) {
      previousWorkspaceIdRef.current = workspaceId;
      setJobs([]);
      setSummary(null);
      setNextCursor(null);
      setUnreachableWorkspaceIds(new Set());
    }
    void load(problemsOnly);
    // Keyed on `serverScope`/`workspaceId` rather than on `load` itself. All
    // three re-run when the scope or workspace moves, which is the fix -- but
    // `load`'s identity also moves with `t`, and depending on it would refetch
    // on every language change. Scope, workspace and filter are the only
    // things that change WHAT to fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverScope, workspaceId, problemsOnly]);

  async function loadMore() {
    if (!nextCursor || loading || loadingMore) {
      return;
    }
    const generation = loadGenerationRef.current;
    const requestedProblemsOnly = problemsOnly;
    setLoadingMore(true);
    try {
      // Paging has to follow the same surface the first page came from;
      // calling the admin endpoint here would 403 for a workspace viewer.
      const result = serverScope
        ? await listAdminJobs({
            problems_only: problemsOnly,
            limit: 50,
            cursor: nextCursor,
          })
        : await (async () => {
            const resolvedWorkspaceId =
              workspaceId;
            if (!resolvedWorkspaceId) {
              throw new Error("no current workspace");
            }
            return listWorkspaceJobs(resolvedWorkspaceId, {
              problems_only: problemsOnly,
              limit: 50,
              cursor: nextCursor,
            });
          })();
      if (
        generation !== loadGenerationRef.current ||
        requestedProblemsOnly !== problemsOnlyRef.current
      ) {
        return;
      }
      setJobs((current) => [...current, ...result.items]);
      setNextCursor(result.next_cursor);
      // Union, not replace: an earlier page may have hit a shard this one
      // did not (or vice versa), and the banner has to stay accurate for
      // every page currently on screen, not just the last one fetched.
      // listWorkspaceJobs' JobListData never has this field (that endpoint
      // never fans out), which is why the cast is gated on serverScope.
      if (serverScope) {
        const unreachableIds = (result as AdminJobListData)
          .unreachable_workspace_ids;
        setUnreachableWorkspaceIds(
          (current) => new Set([...current, ...unreachableIds]),
        );
      }
    } catch (caught) {
      if (
        generation !== loadGenerationRef.current ||
        requestedProblemsOnly !== problemsOnlyRef.current
      ) {
        return;
      }
      setError(
        caught instanceof ApiError
          ? apiErrorMessage(
              caught,
              t("jobs.loadError", { defaultValue: "Could not load jobs." }),
            )
          : t("jobs.loadError", { defaultValue: "Could not load jobs." }),
      );
    } finally {
      if (
        generation === loadGenerationRef.current &&
        requestedProblemsOnly === problemsOnlyRef.current
      ) {
        setLoadingMore(false);
      }
    }
  }

  async function onRetry(job: Job) {
    if (retryingIdRef.current !== null) {
      return;
    }
    retryingIdRef.current = job.id;
    setRetryingId(job.id);
    setRetryError(null);
    try {
      await retryAdminJob(job.id, job.workspace_id);
      // Retrying changes both the aggregate counts and the membership/order of
      // the current page. Reload page one with the active filter so a queued
      // job leaves the problems-only list and an old offset cursor cannot be
      // reused against the changed result set.
      await load(problemsOnlyRef.current);
    } catch (caught) {
      setRetryError(
        caught instanceof ApiError
          ? adminJobRetryErrorMessage(caught, t)
          : t("jobs.retryError.generic", {
              defaultValue: "Retry could not be started. Try again.",
            }),
      );
    } finally {
      retryingIdRef.current = null;
      setRetryingId(null);
    }
  }

  return (
    <section aria-labelledby="admin-jobs-heading" className="feature-section admin-jobs">
      <SettingsPaneHeader
        action={
          <button
            className="icon-button"
            disabled={loading}
            onClick={() => void load(problemsOnly)}
            type="button"
          >
            <RefreshCw aria-hidden="true" size={16} />
            {t("jobs.refresh", { defaultValue: "Refresh" })}
          </button>
        }
        description={t("jobs.description", {
          defaultValue:
            "Background job queue depth, terminal failures, and jobs stuck past their worker lease.",
        })}
        // A server admin reaches Jobs from "This workspace" and can land on a
        // pane headed "Server administration". That is the mode the pane is
        // in, and saying so here is the whole reason the eyebrow exists.
        eyebrow={
          serverScope
            ? t("jobs.eyebrow", { defaultValue: "Server administration" })
            : t("jobs.workspaceEyebrow", { defaultValue: "This workspace" })
        }
        headingId="admin-jobs-heading"
        title={t("jobs.title", { defaultValue: "Jobs" })}
      />

      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      {retryError ? <StatusBanner tone="warning">{retryError}</StatusBanner> : null}
      {unreachableWorkspaceIds.size > 0 ? (
        <StatusBanner tone="warning">
          {t("jobs.incompleteWarning", {
            count: unreachableWorkspaceIds.size,
            defaultValue:
              "{{count}} workspace could not be reached; the totals and list below may be missing its jobs.",
          })}
        </StatusBanner>
      ) : null}

      {summary ? (
        <dl className="admin-jobs-summary">
          <div className="admin-jobs-summary-stat">
            <dt>{t("jobs.summary.queued", { defaultValue: "Queued" })}</dt>
            <dd>{summary.queued_count}</dd>
          </div>
          <div className="admin-jobs-summary-stat">
            <dt>{t("jobs.summary.running", { defaultValue: "Running" })}</dt>
            <dd>{summary.running_count}</dd>
          </div>
          <div className="admin-jobs-summary-stat" data-tone="warn">
            <dt>{t("jobs.summary.failed", { defaultValue: "Failed" })}</dt>
            <dd>{summary.failed_count}</dd>
          </div>
          <div className="admin-jobs-summary-stat" data-tone="warn">
            <dt>
              {t("jobs.summary.leaseExpired", { defaultValue: "Past lease" })}
            </dt>
            <dd>{summary.lease_expired_count}</dd>
          </div>
        </dl>
      ) : null}

      <label className="admin-jobs-filter-toggle" htmlFor="admin-jobs-problems-only">
        <input
          checked={problemsOnly}
          id="admin-jobs-problems-only"
          onChange={(event) => {
            const nextProblemsOnly = event.target.checked;
            problemsOnlyRef.current = nextProblemsOnly;
            setProblemsOnly(nextProblemsOnly);
          }}
          type="checkbox"
        />
        <span>
          {t("jobs.problemsOnly", {
            defaultValue: "Show only failures and jobs past their lease",
          })}
        </span>
      </label>

      {loading ? (
        <SettingsLoadingRow>
          {t("jobs.loading", { defaultValue: "Loading jobs…" })}
        </SettingsLoadingRow>
      ) : jobs.length === 0 ? (
        <div className="empty-state">
          <strong>
            {problemsOnly
              ? t("jobs.emptyProblems", { defaultValue: "No problem jobs" })
              : t("jobs.empty", { defaultValue: "No jobs" })}
          </strong>
          <p>
            {problemsOnly
              ? t("jobs.emptyProblemsHint", {
                  defaultValue: "Nothing is failed or stuck past its lease right now.",
                })
              : t("jobs.emptyHint", { defaultValue: "The queue is empty." })}
          </p>
        </div>
      ) : (
        <ul className="admin-job-list">
          {jobs.map((job) => (
            <li className="admin-job-card" key={job.id}>
              <div className="admin-job-card-main">
                <div className="admin-job-identity">
                  <strong>{job.job_type.replace(/_/g, " ")}</strong>
                  <span>{job.workspace_slug ?? job.workspace_id}</span>
                </div>
                <div className="admin-job-statuses">
                  <StatusPill
                    compact
                    label={t(`jobs.status.${job.status}`, { defaultValue: job.status })}
                    status={job.status}
                    tone={jobStatusTone(job)}
                  />
                  {job.lease_expired ? (
                    <StatusPill
                      compact
                      label={t("jobs.summary.leaseExpired", {
                        defaultValue: "Past lease",
                      })}
                      status="lease_expired"
                      tone="warn"
                    />
                  ) : null}
                </div>
                {job.error_code ? (
                  <p className="admin-job-error">
                    {t(`activity:jobs.errorCode.${job.error_code}`, {
                      defaultValue: t("activity:details.errorCodeFallback", {
                        defaultValue: "The job reported a failure.",
                      }),
                    })}
                  </p>
                ) : null}
                <p className="meta-line">
                  {t("jobs.meta", {
                    defaultValue:
                      "retry {{retryCount}} · updated {{updated}}",
                    retryCount: job.retry_count,
                    updated: formatVersionTimestamp(job.updated_at),
                  })}
                </p>
              </div>
              <div className="admin-job-actions">
                {serverScope ? (
                <button
                  className="icon-button primary"
                  disabled={
                    !job.retryable || job.status !== "failed" || retryingId !== null
                  }
                  onClick={() => void onRetry(job)}
                  type="button"
                >
                  <RotateCcw aria-hidden="true" size={16} />
                  {retryingId === job.id
                    ? t("jobs.retrying", { defaultValue: "Retrying…" })
                    : t("jobs.retry", { defaultValue: "Retry" })}
                </button>
                ) : null}
                {serverScope && job.status === "failed" && !job.retryable ? (
                  <p className="meta-line">
                    {t("jobs.retryUnsupported", {
                      defaultValue:
                        "This job type's failure also updated a domain record; it cannot be retried from here yet.",
                    })}
                  </p>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}

      {nextCursor ? (
        <button
          className="icon-button"
          disabled={loading || loadingMore}
          onClick={() => void loadMore()}
          type="button"
        >
          {loadingMore
            ? t("jobs.loadingMore", { defaultValue: "Loading…" })
            : t("jobs.loadMore", { defaultValue: "Load more" })}
        </button>
      ) : null}
    </section>
  );
}

function jobStatusTone(job: Job): "ok" | "warn" | "muted" {
  if (job.status === "failed" || job.lease_expired) {
    return "warn";
  }
  if (job.status === "completed") {
    return "ok";
  }
  return "muted";
}

function adminJobRetryErrorMessage(
  error: ApiError,
  t: (key: string, options?: Record<string, unknown>) => string,
) {
  if (error.body.error_code === "job_retry_unsupported_type") {
    return t("jobs.retryUnsupported", {
      defaultValue:
        "This job type's failure also updated a domain record; it cannot be retried from here yet.",
    });
  }
  if (error.body.error_code === "job_not_found") {
    return t("jobs.retryError.notFound", {
      defaultValue: "This job is no longer failed. Refresh the list.",
    });
  }
  return apiErrorMessage(
    error,
    t("jobs.retryError.generic", {
      defaultValue: "Retry could not be started. Try again.",
    }),
  );
}
