import { Search, Users } from "lucide-react";
import type { FormEvent } from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  cancelWorkspaceJoin,
  listWorkspaceDirectory,
  requestWorkspaceJoin,
  type WorkspaceDirectoryItem,
  type WorkspaceDirectoryRequestStatus,
} from "../../shared/api/workspaceDirectory";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { Spinner } from "../../shared/components/Spinner";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";

const DIRECTORY_QUERY_MAX_LENGTH = 128;

export function WorkspaceDirectoryScreen() {
  const { t } = useTranslation("shell");
  const [searchParams, setSearchParams] = useSearchParams();
  const activeQuery = (searchParams.get("q") ?? "").trim();
  const activeQueryRef = useRef(activeQuery);
  const [inputQuery, setInputQuery] = useState(activeQuery);
  const [items, setItems] = useState<WorkspaceDirectoryItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [busyWorkspaceId, setBusyWorkspaceId] = useState<string | null>(null);
  const [actionErrors, setActionErrors] = useState<Record<string, boolean>>({});
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    activeQueryRef.current = activeQuery;
    setInputQuery(activeQuery);
  }, [activeQuery]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadingMore(false);
    setLoadError(false);
    setItems([]);
    setNextCursor(null);
    void listWorkspaceDirectory({ query: activeQuery, signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) {
          return;
        }
        setItems(data.items);
        setNextCursor(data.next_cursor);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setLoadError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [activeQuery, reloadVersion]);

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = inputQuery.trim();
    if (query === activeQuery) {
      setReloadVersion((current) => current + 1);
      return;
    }
    setSearchParams(query ? { q: query } : {}, { replace: true });
  }

  async function loadMore() {
    if (!nextCursor || loadingMore) {
      return;
    }
    const queryAtStart = activeQuery;
    setLoadingMore(true);
    setLoadError(false);
    try {
      const data = await listWorkspaceDirectory({
        cursor: nextCursor,
        query: queryAtStart,
      });
      if (activeQueryRef.current !== queryAtStart) {
        return;
      }
      setItems((current) => mergeDirectoryItems(current, data.items));
      setNextCursor(data.next_cursor);
    } catch {
      if (activeQueryRef.current === queryAtStart) {
        setLoadError(true);
      }
    } finally {
      if (activeQueryRef.current === queryAtStart) {
        setLoadingMore(false);
      }
    }
  }

  async function mutateRequest(item: WorkspaceDirectoryItem, action: "request" | "cancel") {
    setBusyWorkspaceId(item.workspace_id);
    setActionErrors((current) => ({ ...current, [item.workspace_id]: false }));
    try {
      const mutation =
        action === "request"
          ? await requestWorkspaceJoin(item.workspace_id)
          : await cancelWorkspaceJoin(item.workspace_id);
      updateItemStatus(item.workspace_id, mutation.request.status);
      setAnnouncement(
        action === "request"
          ? t("workspaceDirectory.announcements.requested", {
              defaultValue: "Join request sent for {{title}}.",
              title: item.title,
            })
          : t("workspaceDirectory.announcements.cancelled", {
              defaultValue: "Join request cancelled for {{title}}.",
              title: item.title,
            }),
      );
    } catch {
      setActionErrors((current) => ({ ...current, [item.workspace_id]: true }));
    } finally {
      setBusyWorkspaceId(null);
    }
  }

  function updateItemStatus(
    workspaceId: string,
    status: WorkspaceDirectoryRequestStatus,
  ) {
    setItems((current) =>
      current.map((item) =>
        item.workspace_id === workspaceId
          ? { ...item, current_request_status: status }
          : item,
      ),
    );
  }

  return (
    <div className="workspace-screen single-column workspace-directory-screen">
      <section className="feature-main">
        <Breadcrumbs
          items={[
            {
              label: t("workspaceDirectory.breadcrumb", {
                defaultValue: "Find workspaces",
              }),
            },
          ]}
        />
        <div className="feature-header workspace-directory-header">
          <div>
            <p className="eyebrow">
              {t("workspaceDirectory.eyebrow", { defaultValue: "Workspace directory" })}
            </p>
            <h1>
              {t("workspaceDirectory.title", { defaultValue: "Find a workspace" })}
            </h1>
            <p className="feature-subtitle">
              {t("workspaceDirectory.description", {
                defaultValue:
                  "Browse discoverable workspaces. Private workspaces and member-only details are never listed here.",
              })}
            </p>
          </div>
        </div>

        <form className="workspace-directory-search" onSubmit={submitSearch} role="search">
          <label htmlFor="workspace-directory-query">
            {t("workspaceDirectory.search.label", {
              defaultValue: "Search workspaces",
            })}
          </label>
          <div>
            <Search size={17} aria-hidden="true" />
            <input
              autoComplete="off"
              id="workspace-directory-query"
              maxLength={DIRECTORY_QUERY_MAX_LENGTH}
              onChange={(event) => setInputQuery(event.target.value)}
              placeholder={t("workspaceDirectory.search.placeholder", {
                defaultValue: "Search by workspace name",
              })}
              type="search"
              value={inputQuery}
            />
            <button className="icon-button primary" type="submit">
              {t("workspaceDirectory.search.action", { defaultValue: "Search" })}
            </button>
          </div>
        </form>

        <p aria-live="polite" className="visually-hidden">
          {announcement}
        </p>

        {loadError && !loading ? (
          <StatusBanner
            action={
              <button
                className="icon-button text-button"
                onClick={() => setReloadVersion((current) => current + 1)}
                type="button"
              >
                {t("workspaceDirectory.retry", { defaultValue: "Retry" })}
              </button>
            }
            tone="warning"
          >
            {t("workspaceDirectory.errors.load", {
              defaultValue: "Could not load the workspace directory.",
            })}
          </StatusBanner>
        ) : null}

        {loading ? (
          <div className="workspace-directory-loading">
            <Spinner
              aria-label={t("workspaceDirectory.loading", {
                defaultValue: "Loading workspace directory",
              })}
            />
            <span>
              {t("workspaceDirectory.loading", {
                defaultValue: "Loading workspace directory",
              })}
            </span>
          </div>
        ) : !loadError && items.length === 0 ? (
          <div className="empty-state empty-state-rich workspace-directory-empty">
            <Users size={24} aria-hidden="true" />
            <h2>
              {activeQuery
                ? t("workspaceDirectory.empty.searchTitle", {
                    defaultValue: "No matching workspaces",
                  })
                : t("workspaceDirectory.empty.title", {
                    defaultValue: "No discoverable workspaces yet",
                  })}
            </h2>
            <p>
              {activeQuery
                ? t("workspaceDirectory.empty.searchDescription", {
                    defaultValue: "Try a different workspace name.",
                  })
                : t("workspaceDirectory.empty.description", {
                    defaultValue:
                      "Only team or public workspaces that you have not joined appear here.",
                  })}
            </p>
          </div>
        ) : items.length > 0 ? (
          <section
            aria-label={t("workspaceDirectory.results", {
              defaultValue: "Workspace results",
            })}
            className="workspace-directory-results"
          >
            {items.map((item) => (
              <WorkspaceDirectoryCard
                actionError={Boolean(actionErrors[item.workspace_id])}
                busy={busyWorkspaceId === item.workspace_id}
                item={item}
                key={item.workspace_id}
                onCancel={() => void mutateRequest(item, "cancel")}
                onRequest={() => void mutateRequest(item, "request")}
              />
            ))}
          </section>
        ) : null}

        {nextCursor && items.length > 0 ? (
          <div className="workspace-directory-pagination">
            <button
              className="icon-button text-button"
              disabled={loadingMore}
              onClick={() => void loadMore()}
              type="button"
            >
              {loadingMore ? <Spinner decorative size={14} /> : null}
              {loadingMore
                ? t("workspaceDirectory.loadingMore", {
                    defaultValue: "Loading more",
                  })
                : t("workspaceDirectory.loadMore", { defaultValue: "Load more" })}
            </button>
          </div>
        ) : null}
      </section>
    </div>
  );
}

function WorkspaceDirectoryCard({
  actionError,
  busy,
  item,
  onCancel,
  onRequest,
}: {
  actionError: boolean;
  busy: boolean;
  item: WorkspaceDirectoryItem;
  onCancel: () => void;
  onRequest: () => void;
}) {
  const { t } = useTranslation("shell");
  const status = item.current_request_status;
  const canRequestAgain =
    status === "rejected" ||
    status === "cancelled" ||
    status === "closed" ||
    // The directory only ever returns workspaces the viewer is not a member
    // of, so an "approved" row means "approved in the past, not currently a
    // member" (e.g. the owner removed them later) — re-requesting is the
    // only meaningful action left (WS-01).
    status === "approved";

  return (
    <article className="workspace-directory-card">
      <div className="workspace-directory-card-heading">
        <div>
          <h2>{item.title}</h2>
          {item.description ? <p>{item.description}</p> : null}
        </div>
        <div className="workspace-directory-badges">
          <StatusPill
            label={t(`workspaceDirectory.visibility.${item.visibility}`, {
              defaultValue: item.visibility === "public" ? "Public" : "Team",
            })}
            status={item.visibility}
            tone={item.visibility === "public" ? "ok" : "info"}
          />
          <StatusPill
            label={t(`workspaceDirectory.policy.${item.join_policy}`, {
              defaultValue:
                item.join_policy === "request" ? "Requests open" : "Invite only",
            })}
            status={item.join_policy}
            tone={item.join_policy === "request" ? "info" : "muted"}
          />
          {status ? (
            <StatusPill
              label={t(`workspaceDirectory.status.${status}`, {
                defaultValue: status,
              })}
              status={status}
            />
          ) : null}
        </div>
      </div>

      <div className="workspace-directory-card-actions">
        {item.join_policy === "invite_only" ? (
          <p>
            {t("workspaceDirectory.actions.inviteOnly", {
              defaultValue: "Ask a workspace owner for an invitation.",
            })}
          </p>
        ) : status === "pending" ? (
          <button
            className="icon-button text-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {busy ? <Spinner decorative size={14} /> : null}
            {t("workspaceDirectory.actions.cancel", {
              defaultValue: "Cancel request",
            })}
          </button>
        ) : (
          <button
            className="icon-button primary"
            disabled={busy}
            onClick={onRequest}
            type="button"
          >
            {busy ? <Spinner decorative size={14} /> : null}
            {canRequestAgain
              ? t("workspaceDirectory.actions.requestAgain", {
                  defaultValue: "Request again",
                })
              : t("workspaceDirectory.actions.request", {
                  defaultValue: "Request to join",
                })}
          </button>
        )}
        {actionError ? (
          <p className="workspace-directory-action-error" role="alert">
            {t("workspaceDirectory.errors.action", {
              defaultValue: "Could not update this join request. Try again.",
            })}
          </p>
        ) : null}
      </div>
    </article>
  );
}

function mergeDirectoryItems(
  current: WorkspaceDirectoryItem[],
  incoming: WorkspaceDirectoryItem[],
) {
  const byId = new Map(current.map((item) => [item.workspace_id, item]));
  for (const item of incoming) {
    byId.set(item.workspace_id, item);
  }
  return [...byId.values()];
}
