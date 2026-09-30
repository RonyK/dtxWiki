import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  apiErrorMessage,
  getApiBaseUrl,
} from "../../shared/api/client";
import { deletePageContextMedia } from "../../shared/api/contextMedia";
import {
  listWorkspaceContextMediaAttachments,
  type ContextMediaAttachment,
} from "../../shared/api/contextMediaAttachments";
import {
  listAdminContextMediaOrphans,
  type ContextMediaOrphan,
} from "../../shared/api/contextMediaOrphans";
import { getCurrentWorkspaceId } from "../../shared/api/workspace";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import { SearchField } from "../../shared/components/SearchField";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { ImageViewerDialog } from "../../shared/components/ImageViewerDialog";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import {
  SettingsEmptyState,
  SettingsLoadingRow,
  SettingsPaneHeader,
} from "./SettingsPaneParts";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import {
  formatMediaBytes,
  mediaDeleteErrorKind,
  type MediaDeleteErrorKind,
} from "../../shared/media/mediaFormatting";

// Every image, audio and video file the pages of this workspace hold.
//
// Replaces the orphan-only screen. Orphans stay first-class -- they are one
// filter here rather than the whole page -- because "which uploads is nobody
// using" is a real question, just not the only one: nothing previously
// answered "what have I uploaded" or "what is that file on that page".
//
// `scope="server"` is the reduced fallback for a server admin who is not a
// member of this workspace. The attachment endpoint requires membership, so
// that reader keeps the untouched admin orphan endpoint: orphans only, no
// previews and no delete, because that payload carries neither a
// `content_url` nor a `usage_token`. Dropping it would take a capability away
// from users who have it today.
export type AttachmentsScope = "server" | "workspace";

type AttachmentsFilter = "all" | "mine" | "orphans";

/** Where focus goes once the inline confirmation closes. */
type PendingFocus =
  | { kind: "trigger"; assetId: string }
  | { kind: "heading" };

const PAGE_SIZE = 24;

export function AttachmentsSection({
  scope = "workspace",
  workspaceId = null,
  canViewEveryUpload = false,
}: {
  scope?: AttachmentsScope;
  // Explicit for both scopes. Reading the module-global current workspace
  // inside load() instead is what once left one workspace's filenames and
  // uploader names on screen after the selection had already moved.
  workspaceId?: string | null;
  // Owner of the selected workspace -- the only role the server answers
  // `scope=workspace` for. Gates the "All" segment so the UI never offers a
  // request it already knows returns 403.
  canViewEveryUpload?: boolean;
}) {
  const { t, i18n } = useTranslation("settings");
  const serverScope = scope === "server";
  const [filter, setFilter] = useState<AttachmentsFilter>(
    canViewEveryUpload ? "all" : "mine",
  );
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [items, setItems] = useState<ContextMediaAttachment[]>([]);
  const [orphanRows, setOrphanRows] = useState<ContextMediaOrphan[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [viewerAssetId, setViewerAssetId] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<MediaDeleteErrorKind | null>(
    null,
  );
  const [unpreviewable, setUnpreviewable] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const loadGenerationRef = useRef(0);
  const previousWorkspaceIdRef = useRef(workspaceId);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const confirmCancelRef = useRef<HTMLButtonElement | null>(null);
  const deleteTriggerRefs = useRef(new Map<string, HTMLButtonElement>());
  const pendingFocusRef = useRef<PendingFocus | null>(null);

  const byteLabels = {
    bytes: (value: string) =>
      t("attachments.size.bytes", { defaultValue: "{{value}} B", value }),
    kilobytes: (value: string) =>
      t("attachments.size.kilobytes", { defaultValue: "{{value}} KB", value }),
    megabytes: (value: string) =>
      t("attachments.size.megabytes", { defaultValue: "{{value}} MB", value }),
  };

  const load = useCallback(
    async (cursor: string | null) => {
      const generation = loadGenerationRef.current + 1;
      loadGenerationRef.current = generation;
      if (cursor) {
        setLoadingMore(true);
      } else {
        setLoading(true);
        // A fresh first page replaces every row, so an open confirmation is
        // now pointed at a row the refetch may have replaced -- and after a
        // 412 the copy literally tells the reader to refresh, so leaving the
        // stale panel and its stale error standing contradicts the advice.
        setPendingDeleteId(null);
        setDeleteError(null);
      }
      setError(null);
      try {
        // The prop is authoritative when the host knows the selection; the
        // global stays the fallback for hosts that render this with no
        // workspace context at all, where there is no selection to go stale.
        const resolved = workspaceId ?? (await getCurrentWorkspaceId());
        if (!resolved) {
          throw new Error("no current workspace");
        }
        if (serverScope) {
          const rows = await listAdminContextMediaOrphans(resolved);
          if (generation !== loadGenerationRef.current) return;
          setOrphanRows(rows);
          setNextCursor(null);
          return;
        }
        const page = await listWorkspaceContextMediaAttachments(resolved, {
          // Only an owner may ask for `workspace`, and only "Mine" is
          // deliberately narrow. "Unused" stays workspace-wide for an owner:
          // cleaning up storage is a workspace job, and scoping it to the
          // owner's own uploads would make this screen answer LESS than the
          // orphan screen it replaces.
          scope:
            filter === "mine" || !canViewEveryUpload ? "mine" : "workspace",
          orphanOnly: filter === "orphans",
          query: appliedSearch,
          limit: PAGE_SIZE,
          cursor: cursor ?? undefined,
        });
        if (generation !== loadGenerationRef.current) return;
        setItems((current) =>
          cursor ? [...current, ...page.items] : page.items,
        );
        setNextCursor(page.nextCursor);
      } catch (caught) {
        if (generation !== loadGenerationRef.current) return;
        const fallback = t("attachments.loadError", {
          defaultValue: "Could not load attachments.",
        });
        setError(
          caught instanceof ApiError
            ? apiErrorMessage(caught, fallback)
            : fallback,
        );
      } finally {
        if (generation === loadGenerationRef.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [appliedSearch, canViewEveryUpload, filter, serverScope, t, workspaceId],
  );

  useEffect(() => {
    // Clear before refetching, not after: these rows carry other people's
    // filenames and thumbnails, and the generation fence can only stop a late
    // response from painting -- it cannot un-render what is already there.
    if (previousWorkspaceIdRef.current !== workspaceId) {
      previousWorkspaceIdRef.current = workspaceId;
      setItems([]);
      setOrphanRows([]);
      setNextCursor(null);
      setError(null);
      setPendingDeleteId(null);
      setUnpreviewable(new Set());
    }
    void load(null);
  }, [load, workspaceId]);

  useEffect(() => {
    // Opening the panel removes the button that had focus, and closing it
    // removes the button that has it now. Neither React nor the browser puts
    // focus anywhere sensible on its own -- it lands on <body>, which for a
    // keyboard reader means starting over from the top of the page.
    if (pendingDeleteId) {
      confirmCancelRef.current?.focus();
      return;
    }
    const target = pendingFocusRef.current;
    if (!target) return;
    pendingFocusRef.current = null;
    if (target.kind === "trigger") {
      deleteTriggerRefs.current.get(target.assetId)?.focus();
    } else {
      headingRef.current?.focus();
    }
  }, [items, pendingDeleteId]);

  async function confirmDelete(item: ContextMediaAttachment) {
    const resolved = workspaceId ?? (await getCurrentWorkspaceId());
    if (!resolved) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      // The usage_token this row was listed with. A 412 means somebody
      // referenced or edited the asset in between; a 409 means it is still in
      // use -- reachable even from a row that said can_delete, because that
      // flag derives from viewer-MASKED usage.
      await deletePageContextMedia(resolved, item.asset_id, item.usage_token);
      // The row the trigger lived on is about to disappear; the section
      // heading is the nearest thing that survives.
      pendingFocusRef.current = { kind: "heading" };
      setPendingDeleteId(null);
      await load(null);
    } catch (caught) {
      setDeleteError(mediaDeleteErrorKind(caught));
    } finally {
      setDeleteBusy(false);
    }
  }

  function applySearch(next: string) {
    setAppliedSearch(next);
    setNextCursor(null);
  }

  const filterOptions = [
    ...(canViewEveryUpload
      ? [
          {
            key: "all" as const,
            label: t("attachments.filters.all", { defaultValue: "All" }),
          },
        ]
      : []),
    {
      key: "mine" as const,
      label: t("attachments.filters.mine", { defaultValue: "Mine" }),
    },
    {
      key: "orphans" as const,
      label: t("attachments.filters.orphans", { defaultValue: "Unused" }),
    },
  ];

  const viewer =
    viewerAssetId === null
      ? null
      : (items.find((item) => item.asset_id === viewerAssetId) ?? null);
  const rowCount = serverScope ? orphanRows.length : items.length;

  return (
    <section
      aria-labelledby="attachments-heading"
      className="feature-section attachments"
    >
      <SettingsPaneHeader
        action={
          <button
            className="icon-button"
            disabled={loading}
            onClick={() => void load(null)}
            type="button"
          >
            <RefreshCw aria-hidden="true" size={16} />
            {t("attachments.refresh", { defaultValue: "Refresh" })}
          </button>
        }
        description={t("attachments.description", {
          defaultValue:
            "Images, audio and video uploaded into this workspace's pages.",
        })}
        eyebrow={
          serverScope
            ? t("attachments.serverEyebrow", {
                defaultValue: "Server administration",
              })
            : t("attachments.workspaceEyebrow", {
                defaultValue: "This workspace",
              })
        }
        headingId="attachments-heading"
        // Deleting the last row sends focus back here, so the ref has to reach
        // the heading the shared header renders.
        headingRef={headingRef}
        title={t("attachments.title", { defaultValue: "Attachments" })}
      />

      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}

      {serverScope ? (
        <StatusBanner tone="info">
          {t("attachments.serverAdminFallbackNotice", {
            defaultValue:
              "You are not a member of this workspace, so this lists unused uploads only, without previews or deletion. Join the workspace to manage its attachments.",
          })}
        </StatusBanner>
      ) : (
        <div className="attachments-controls">
          <SegmentedControl
            compact
            label={t("attachments.filters.label", {
              defaultValue: "Which attachments",
            })}
            options={filterOptions}
            value={filter}
            onChange={(next) => {
              setFilter(next);
              setNextCursor(null);
              setPendingDeleteId(null);
            }}
          />
          <SearchField
            ariaLabel={t("attachments.search.label", {
              defaultValue: "Search attachments by file name",
            })}
            clearLabel={t("attachments.search.clear", {
              defaultValue: "Clear search",
            })}
            compact
            maxLength={200}
            onChange={setSearch}
            onClear={() => applySearch("")}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                // This filter sits inside the Settings form; Enter must run
                // the search rather than trip that form's implicit submit.
                event.preventDefault();
                applySearch(search);
              }
            }}
            placeholder={t("attachments.search.placeholder", {
              defaultValue: "File name",
            })}
            submitLabel={t("attachments.search.submit", {
              defaultValue: "Search",
            })}
            value={search}
          />
        </div>
      )}

      {!serverScope && filter === "orphans" ? (
        <StatusBanner tone="info">
          {t("attachments.retentionNotice", {
            defaultValue:
              "Unused uploads are reclaimed automatically once they pass the server's retention window. Deleting one here only brings that forward.",
          })}
        </StatusBanner>
      ) : null}

      {!serverScope && filter === "all" ? (
        <p className="field-hint">
          {t("attachments.allScopeNotice", {
            defaultValue:
              "Every attachment except those staged in someone else's unsaved draft — an open draft stays private to its author until it is saved.",
          })}
        </p>
      ) : null}

      {!serverScope && filter === "mine" && !canViewEveryUpload ? (
        <p className="field-hint">
          {t("attachments.mineScopeNotice", {
            defaultValue:
              "These are the attachments you uploaded. Workspace owners can see every member's.",
          })}
        </p>
      ) : null}

      {loading ? (
        <SettingsLoadingRow>
          {t("attachments.loading", { defaultValue: "Loading attachments…" })}
        </SettingsLoadingRow>
      ) : rowCount === 0 ? (
        <SettingsEmptyState
          description={emptyHint(serverScope, filter, appliedSearch, t)}
          title={emptyTitle(serverScope, filter, appliedSearch, t)}
        />
      ) : (
        <>
          <ListPanelHeader
            approxMore={Boolean(nextCursor)}
            count={rowCount}
            // Naming the active filter rather than repeating the section's own
            // "Attachments" heading two lines above it: the count means
            // nothing without knowing what it counted.
            title={t(
              `attachments.listTitle.${serverScope ? "orphans" : filter}`,
              { defaultValue: LIST_TITLE_FALLBACK[serverScope ? "orphans" : filter] },
            )}
            // One localized string rather than "<n> <unit>": Korean writes
            // "5개" with no space, which a composed label cannot express.
            countText={t("attachments.countText", {
              count: rowCount,
              defaultValue_one: "{{count}} file",
              defaultValue_other: "{{count}} files",
            })}
          />
          <ul className="attachments-list">
            {serverScope
              ? orphanRows.map((row) => (
                  <li className="attachments-card" key={row.asset_id}>
                    <div className="attachments-card-main">
                      <div className="attachments-identity">
                        <strong>{row.original_filename}</strong>
                        <span className="meta-line">
                          {t("attachments.fileMeta", {
                            defaultValue: "{{mediaType}} · {{size}}",
                            mediaType: mediaTypeLabel(row.media_type),
                            size: formatMediaBytes(
                              row.bytes,
                              i18n.language,
                              byteLabels,
                            ),
                          })}
                        </span>
                      </div>
                      <dl className="attachments-facts">
                        <div>
                          <dt>
                            {t("attachments.facts.page", {
                              defaultValue: "Page",
                            })}
                          </dt>
                          <dd>{row.page_title ?? row.page_id}</dd>
                        </div>
                        <div>
                          <dt>
                            {t("attachments.facts.uploadedBy", {
                              defaultValue: "Uploaded by",
                            })}
                          </dt>
                          <dd>{row.uploaded_by_display_name}</dd>
                        </div>
                        <div>
                          <dt>
                            {t("attachments.facts.uploadedAt", {
                              defaultValue: "Uploaded",
                            })}
                          </dt>
                          <dd>{formatVersionTimestamp(row.created_at)}</dd>
                        </div>
                      </dl>
                    </div>
                  </li>
                ))
              : items.map((item) => {
                  const state = attachmentState(item);
                  return (
                    <li className="attachments-card" key={item.asset_id}>
                      {previewable(item, unpreviewable) ? (
                        // The payload carries a root-relative path. That is
                        // right when the app is served from the API's own
                        // origin and wrong the moment it is not (a Vite dev
                        // server, a packaged shell pointed at a remote API):
                        // the browser asks the wrong host and paints a
                        // broken-image glyph. getApiBaseUrl() is "" in the
                        // same-origin case, so this stays relative there.
                        <button
                          aria-expanded={viewerAssetId === item.asset_id}
                          aria-haspopup="dialog"
                          aria-label={t("attachments.expandPreview", {
                            defaultValue: "Expand {{filename}}",
                            filename: item.original_filename,
                          })}
                          className="attachments-preview-trigger"
                          type="button"
                          onClick={() => setViewerAssetId(item.asset_id)}
                        >
                        <img
                          alt=""
                          className="attachments-preview"
                          loading="lazy"
                          onError={() =>
                            setUnpreviewable((current) =>
                              new Set(current).add(item.asset_id),
                            )
                          }
                          src={`${getApiBaseUrl()}${item.content_url}`}
                        />
                        </button>
                      ) : (
                        <span
                          aria-hidden="true"
                          className="attachments-preview attachments-preview-kind"
                          title={previewPlaceholderHint(item, t)}
                        >
                          {placeholderGlyph(item)}
                        </span>
                      )}
                      <div className="attachments-card-main">
                        <div className="attachments-identity">
                          <strong title={item.original_filename}>
                            {item.original_filename}
                          </strong>
                          <div className="attachments-meta-row">
                            <span className="meta-line">
                              {t("attachments.fileMeta", {
                                defaultValue: "{{mediaType}} · {{size}}",
                                mediaType: mediaTypeLabel(item.media_type),
                                size: formatMediaBytes(
                                  item.bytes,
                                  i18n.language,
                                  byteLabels,
                                ),
                              })}
                            </span>
                            <div className="attachments-badges">
                              {/* ONE state pill. usage_state=unused and is_orphan
                                  overlap -- every orphan is unused -- so rendering
                                  both put two pills reading "Unused" side by side.
                                  What actually distinguishes them is whether an
                                  open edit is still holding the file, and that is
                                  what this says. */}
                              <StatusPill
                                compact
                                label={t(`attachments.state.${state}`, {
                                  defaultValue: STATE_FALLBACK[state],
                                })}
                                status={state}
                                title={t(`attachments.stateHint.${state}`, {
                                  defaultValue: STATE_HINT_FALLBACK[state],
                                })}
                                tone={STATE_TONE[state]}
                              />
                              {item.is_mine ? (
                                <StatusPill
                                  compact
                                  label={t("attachments.mineBadge", {
                                    defaultValue: "You",
                                  })}
                                  status="info"
                                  tone="info"
                                />
                              ) : null}
                              {/* "Active" is the state of nearly every row, so
                                  showing it would be noise on every card. The two
                                  states an operator can act on are the ones worth
                                  a pill. */}
                              {item.association_state === "active" ? null : (
                                <StatusPill
                                  compact
                                  label={t(
                                    `attachments.associationState.${item.association_state}`,
                                    {
                                      defaultValue: associationStateFallback(
                                        item.association_state,
                                      ),
                                    },
                                  )}
                                  status={item.association_state}
                                />
                            )}
                            </div>
                          </div>
                        </div>

                        <dl className="attachments-facts">
                          <div>
                            <dt className="visually-hidden">
                              {t("attachments.facts.page", {
                                defaultValue: "Page",
                              })}
                            </dt>
                            <dd>
                              {/* The title can still be null if the row
                                  vanished between the listing and its title
                                  lookup, so the id stays the fallback rather
                                  than a blank cell. */}
                              <a
                                className="attachments-page-link"
                                href={`/context?target=${encodeURIComponent(item.page_id)}`}
                                title={item.page_id}
                              >
                                {item.page_title ?? item.page_id}
                              </a>
                            </dd>
                          </div>
                          <div>
                            <dt className="visually-hidden">
                              {t("attachments.facts.uploadedBy", {
                                defaultValue: "Uploaded by",
                              })}
                            </dt>
                            <dd>{item.uploaded_by_display_name}</dd>
                          </div>
                          <div>
                            <dt className="visually-hidden">
                              {t("attachments.facts.uploadedAt", {
                                defaultValue: "Uploaded",
                              })}
                            </dt>
                            <dd>{formatVersionTimestamp(item.created_at)}</dd>
                          </div>
                        </dl>

                        {item.can_delete ? (
                          pendingDeleteId === item.asset_id ? (
                            <div className="attachments-confirm">
                              <p className="field-hint">
                                {t("attachments.confirmDelete.body", {
                                  defaultValue:
                                    "Permanently delete {{filename}}? This cannot be undone.",
                                  filename: item.original_filename,
                                })}
                              </p>
                              {deleteError ? (
                                <p className="form-error">
                                  {t(`attachments.errors.${deleteError}`, {
                                    defaultValue:
                                      "Could not delete this attachment. Try again.",
                                  })}
                                </p>
                              ) : null}
                              <div className="attachments-confirm-actions">
                                <button
                                  className="icon-button"
                                  disabled={deleteBusy}
                                  onClick={() => {
                                    pendingFocusRef.current = {
                                      kind: "trigger",
                                      assetId: item.asset_id,
                                    };
                                    setPendingDeleteId(null);
                                    setDeleteError(null);
                                  }}
                                  ref={confirmCancelRef}
                                  type="button"
                                >
                                  {t("attachments.confirmDelete.cancel", {
                                    defaultValue: "Cancel",
                                  })}
                                </button>
                                <button
                                  className="icon-button danger-action"
                                  disabled={deleteBusy}
                                  onClick={() => void confirmDelete(item)}
                                  type="button"
                                >
                                  {deleteBusy
                                    ? t("attachments.actions.deleting", {
                                        defaultValue: "Deleting…",
                                      })
                                    : t("attachments.confirmDelete.confirm", {
                                        defaultValue: "Delete attachment",
                                      })}
                                </button>
                              </div>
                            </div>
                          ) : (
                            <div className="attachments-actions">
                              <button
                                className="icon-button danger-action"
                                onClick={() => {
                                  setDeleteError(null);
                                  setPendingDeleteId(item.asset_id);
                                }}
                                ref={(node) => {
                                  if (node) {
                                    deleteTriggerRefs.current.set(
                                      item.asset_id,
                                      node,
                                    );
                                  } else {
                                    deleteTriggerRefs.current.delete(
                                      item.asset_id,
                                    );
                                  }
                                }}
                                type="button"
                              >
                                {t("attachments.actions.delete", {
                                  defaultValue: "Delete",
                                })}
                              </button>
                            </div>
                          )
                        ) : null}
                      </div>
                    </li>
                  );
                })}
          </ul>
          {nextCursor ? (
            <button
              className="icon-button attachments-load-more"
              disabled={loadingMore}
              onClick={() => void load(nextCursor)}
              type="button"
            >
              {loadingMore
                ? t("attachments.actions.loadingMore", {
                    defaultValue: "Loading…",
                  })
                : t("attachments.actions.loadMore", {
                    defaultValue: "Load more",
                  })}
            </button>
          ) : null}
        </>
      )}
      {/* One viewer for the whole section, driven by which row was opened --
          rather than one mounted per row, which would portal a dialog for
          every attachment on screen. */}
      {viewer ? (
        <ImageViewerDialog
          alt={viewer.original_filename}
          closeLabel={t("attachments.closePreview", {
            defaultValue: "Close preview",
          })}
          src={`${getApiBaseUrl()}${viewer.content_url}`}
          title={t("attachments.previewViewerTitle", {
            defaultValue: "{{filename}} preview",
            filename: viewer.original_filename,
          })}
          titleId="attachments-preview-viewer-title"
          onClose={() => setViewerAssetId(null)}
          onError={() => {
            // Same failure the thumbnail already handles: mark it
            // unpreviewable so the card stops offering an enlargement it
            // cannot produce, and close rather than leave an empty dialog.
            setUnpreviewable((current) => new Set(current).add(viewer.asset_id));
            setViewerAssetId(null);
          }}
        />
      ) : null}
    </section>
  );
}

type Translate = ReturnType<typeof useTranslation>["t"];

// An unused upload's BYTES stay private to whoever uploaded it: it was
// never published as content, so the content route answers 404 to everyone
// else even while the row itself is listed (ResolveContent.version says so
// in as many words). Asking anyway paints the browser's broken-image glyph,
// which reads as "this file is corrupt" rather than "this preview is not
// yours". A non-image has no still to show either. The onError set is the
// backstop for every other reason a fetch can fail.
function previewable(
  item: ContextMediaAttachment,
  unpreviewable: ReadonlySet<string>,
): boolean {
  if (item.kind !== "image") return false;
  if (unpreviewable.has(item.asset_id)) return false;
  return !item.is_orphan || item.is_mine;
}

function placeholderGlyph(item: ContextMediaAttachment): string {
  if (item.kind === "audio") return "♪";
  if (item.kind === "video") return "▶";
  return "□";
}

function previewPlaceholderHint(item: ContextMediaAttachment, t: Translate) {
  if (item.kind === "image") {
    return t("attachments.previewPrivate", {
      defaultValue:
        "No preview: an unused upload stays readable only by the person who uploaded it.",
    });
  }
  return t("attachments.previewNotAnImage", {
    defaultValue: "No preview for this file type.",
  });
}

// One generic "nothing here" cannot tell an operator whether the workspace is
// empty, whether their own uploads are, or whether their query simply missed.
function emptyTitle(
  serverScope: boolean,
  filter: AttachmentsFilter,
  search: string,
  t: Translate,
) {
  if (search && !serverScope) {
    return t("attachments.empty.search", {
      defaultValue: "No attachment matches that name",
    });
  }
  if (serverScope || filter === "orphans") {
    return t("attachments.empty.orphans", {
      defaultValue: "Nothing to clean up",
    });
  }
  if (filter === "mine") {
    return t("attachments.empty.mine", {
      defaultValue: "You have not uploaded anything yet",
    });
  }
  return t("attachments.empty.all", { defaultValue: "No attachments yet" });
}

function emptyHint(
  serverScope: boolean,
  filter: AttachmentsFilter,
  search: string,
  t: Translate,
) {
  if (search && !serverScope) {
    return t("attachments.emptyHint.search", {
      defaultValue: "Clear the search box to see every attachment again.",
    });
  }
  if (serverScope || filter === "orphans") {
    return t("attachments.emptyHint.orphans", {
      defaultValue:
        "Every uploaded file in this workspace is either published, in a draft, or still an upload in progress.",
    });
  }
  if (filter === "mine") {
    return t("attachments.emptyHint.mine", {
      defaultValue:
        "Images you drop into a page you are editing appear here once they upload.",
    });
  }
  return t("attachments.emptyHint.all", {
    defaultValue: "Images added to any page in this workspace appear here.",
  });
}

function mediaTypeLabel(mediaType: string) {
  return mediaType.split("/")[1]?.toUpperCase() ?? mediaType;
}

function associationStateFallback(associationState: string) {
  if (associationState === "pending_install") return "Pending install";
  if (associationState === "tombstoned") return "Removed";
  return associationState.replace(/_/g, " ");
}

const LIST_TITLE_FALLBACK: Record<AttachmentsFilter, string> = {
  all: "Every attachment",
  mine: "Your uploads",
  orphans: "Unused uploads",
};

type AttachmentState = "inUse" | "historyOnly" | "staging" | "unused";

const STATE_FALLBACK: Record<AttachmentState, string> = {
  inUse: "In use",
  historyOnly: "History only",
  staging: "In an open draft",
  unused: "Unused",
};

const STATE_HINT_FALLBACK: Record<AttachmentState, string> = {
  inUse: "Shown on the current published version of its page.",
  historyOnly: "Only referenced by older versions of its page.",
  staging: "Uploaded into an edit nobody has saved yet, so it is held.",
  unused: "No version of its page references it; the retention sweep will reclaim it.",
};

const STATE_TONE: Record<AttachmentState, string> = {
  inUse: "ok",
  historyOnly: "muted",
  staging: "info",
  unused: "warn",
};

/**
 * The four states a reader can act on, from two overlapping backend fields.
 *
 * `usage_state` alone cannot tell "nobody references this, reclaim it" from
 * "nobody references this YET because the edit that uploaded it is still
 * open" -- both are `unused`. `is_orphan` is what separates them, and that
 * difference is the whole point of the Unused filter.
 */
function attachmentState(item: ContextMediaAttachment): AttachmentState {
  if (item.usage_state === "active") return "inUse";
  if (item.usage_state === "history_only") return "historyOnly";
  return item.is_orphan ? "unused" : "staging";
}
