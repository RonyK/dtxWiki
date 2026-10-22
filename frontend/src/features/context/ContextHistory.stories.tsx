import { ContextHistory } from "./ContextScreen";
import type { ContextChangeHistory, ContextPageDetail, VersionEntry } from "../../shared/api/context";
import type { ContextHistoryStatus } from "../../shared/lib/contextHistory";

/**
 * Isolated render of the History tab's version list -- no context/fetch/router,
 * so a story mounts it directly against a hand-built fixture. Wrapped in the
 * production `.context-screen > .context-reader` column (max 720px, fluid
 * below that -- matching the `@media (max-width: 720px)` compaction the CSS
 * itself keys on) so it picks up the same cascade and width the real reader
 * gives it; `--reader-accent`
 * is left unset, which resolves to `--context` exactly as it does in
 * production (Context sets no other accent on this tab).
 *
 * What this does NOT prove: the real tab panel's own width constraints,
 * ancestor `overflow`, or the API payload's actual null-ability. The
 * stylesheet and the DOM are the real ones, so gutter continuity and the
 * card's internal layout -- what these stories exist to check -- render
 * exactly as shipped.
 */
function ReaderColumn({ children }: { children: React.ReactNode }) {
  return (
    <div className="context-screen">
      <div className="context-reader" style={{ width: "100%", maxWidth: 720 }}>
        {children}
      </div>
    </div>
  );
}

function detail(overrides: Partial<ContextPageDetail> = {}): ContextPageDetail {
  return {
    id: "page-1",
    slug: "decision-log",
    title: "Decision Log",
    page_type: "decision_log",
    lifecycle_status: "published",
    aliases: [],
    visible_version_id: "version-2",
    current_version_id: "version-2",
    current_published_version_id: "version-2",
    latest_draft_version_id: null,
    is_public: false,
    updated_at: "2026-05-09T00:00:00Z",
    workspace_id: "workspace-1",
    body_markdown: null,
    version_info: null,
    tree_path: [],
    parent_node_id: null,
    children_count: 0,
    backlink_count: 0,
    wikilink_targets: [],
    warnings: [],
    pending_draft_version_id: null,
    pending_draft_warnings: [],
    pending_review: null,
    reconciliation_pending_count: 0,
    reconciliation_pending_count_truncated: false,
    next_reconciliation_version_id: null,
    reconciliation_available: false,
    ...overrides,
  };
}

function historyVersion(
  id: string,
  versionNo: number,
  historyStatus: ContextHistoryStatus,
  overrides: Partial<VersionEntry> = {},
): VersionEntry {
  const priorVersionId = versionNo > 1 ? `version-${versionNo - 1}` : null;
  return {
    version_id: id,
    version_no: versionNo,
    history_status: historyStatus,
    graph_status:
      historyStatus === "alternative_draft"
        ? "branched"
        : historyStatus === "discarded_draft"
          ? "discarded"
          : "linear",
    actor_type: "human",
    actor_display_name: "Alice",
    created_at: "2026-05-09T00:00:00Z",
    prior_version_id: priorVersionId,
    prior_relation: priorVersionId ? "visible" : "root",
    actor_id: null,
    lines_added: versionNo * 12,
    lines_removed: 3,
    review_disposition: historyStatus === "current_draft" ? "pending" : "approved",
    is_current: false,
    is_current_published: false,
    is_latest_draft: false,
    review_status: historyStatus === "current_draft" ? "pending" : "approved",
    review_item_id: `review-${id}`,
    is_visible_head: false,
    is_published_head: false,
    is_draft_head: false,
    is_alternative: false,
    is_discarded: false,
    ...overrides,
  } as VersionEntry;
}

function history(versions: VersionEntry[]): ContextChangeHistory {
  return {
    versions,
    changesets: [],
    review_decisions: [],
    diffs: null,
    next_cursor: null,
  };
}

const baseProps = {
  canEdit: true,
  comparePair: null,
  compareStateError: null,
  historyLoadError: null,
  historyLoadingOlder: false,
  highlightedVersionId: null,
  onCloseCompare: () => {},
  onCompare: () => {},
  onComparePair: () => {},
  onLoadOlder: () => {},
  onReconcile: () => {},
  onVersionRestored: () => {},
  workspaceId: "workspace-1",
};

const meta = {
  title: "Context/History",
  component: ContextHistory,
};

export default meta;

// Two versions, one lineage -- the control group this replaces nothing about,
// used as a visual baseline against ForkAndMerge and States.
export const Linear = {
  render: () => (
    <ReaderColumn>
      <ContextHistory
        {...baseProps}
        detail={detail()}
        history={history([
          historyVersion("version-2", 2, "current_draft", {
            is_visible_head: true,
            is_draft_head: true,
          }),
          historyVersion("version-1", 1, "published_snapshot", {
            is_current_published: true,
          }),
        ])}
      />
    </ReaderColumn>
  ),
};

// A fork and its reconcile merge at once. v1 -> v2 -> v4 -> v5 is the trunk;
// v3 forks off v1 (alternative_draft) and never grows descendants; v5 is the
// reconcile-merge version, and `merged_into_version_id` (set on v3, the
// branch) is its second parent. v3 keeps its own lane from v5's merge
// departure all the way down to the row directly above v1 and joins there --
// it must never run down the trunk through v2's dot, which is how this story
// rendered before 2026-10-22 and why it is not a reference picture on its own:
// compare it with `git log --graph --date-order` for the same parent links
// (see the HISTORY GRAPH CONTRACT in shared/lib/historyGraph.ts).
export const ForkAndMerge = {
  render: () => (
    <ReaderColumn>
      <ContextHistory
        {...baseProps}
        detail={detail({
          current_version_id: "version-5",
          current_published_version_id: "version-5",
          visible_version_id: "version-5",
        })}
        history={history([
          historyVersion("version-5", 5, "published_snapshot", {
            is_visible_head: true,
            is_current_published: true,
          }),
          historyVersion("version-4", 4, "superseded_snapshot", {
            prior_version_id: "version-2",
          }),
          historyVersion("version-3", 3, "alternative_draft", {
            prior_version_id: "version-1",
            merged_into_version_id: "version-5",
          }),
          historyVersion("version-2", 2, "superseded_snapshot"),
          historyVersion("version-1", 1, "published_snapshot"),
        ])}
      />
    </ReaderColumn>
  ),
};

// The 2026-10-22 report: two drafts forked from the same published v3. v4 is
// the draft head (trunk), v5 an alternative. v5's edge keeps lane 1 past v4
// and joins v3 only at the bottom of v4's row (`|/`); it must not cross v4's
// dot, which is what made this read as one straight v3 -> v4 -> v5 line.
export const SameBaseFork = {
  render: () => (
    <ReaderColumn>
      <ContextHistory
        {...baseProps}
        detail={detail({
          current_version_id: "version-4",
          current_published_version_id: "version-3",
          latest_draft_version_id: "version-4",
          visible_version_id: "version-4",
        })}
        history={history([
          historyVersion("version-5", 5, "alternative_draft", {
            prior_version_id: "version-3",
            is_alternative: true,
          }),
          historyVersion("version-4", 4, "current_draft", {
            prior_version_id: "version-3",
            is_draft_head: true,
            is_visible_head: true,
          }),
          historyVersion("version-3", 3, "published_snapshot", {
            is_current_published: true,
          }),
          historyVersion("version-2", 2, "superseded_snapshot"),
          historyVersion("version-1", 1, "superseded_snapshot"),
        ])}
      />
    </ReaderColumn>
  ),
};

// Three versions forked from v1 while all three are still alive: three lanes,
// joining v1 together in the bottom of v2's row. Sharing a lane between any
// two of them (the pre-2026-10-22 "siblings never sit side by side" packing)
// erases the fork.
export const ThreeWayFork = {
  render: () => (
    <ReaderColumn>
      <ContextHistory
        {...baseProps}
        detail={detail({
          current_version_id: "version-4",
          current_published_version_id: "version-1",
          latest_draft_version_id: "version-4",
          visible_version_id: "version-4",
        })}
        history={history([
          historyVersion("version-4", 4, "current_draft", {
            prior_version_id: "version-1",
            is_draft_head: true,
            is_visible_head: true,
          }),
          historyVersion("version-3", 3, "alternative_draft", {
            prior_version_id: "version-1",
            is_alternative: true,
          }),
          historyVersion("version-2", 2, "discarded_draft", {
            prior_version_id: "version-1",
            is_discarded: true,
          }),
          historyVersion("version-1", 1, "published_snapshot", {
            is_current_published: true,
          }),
        ])}
      />
    </ReaderColumn>
  ),
};

// First page of a longer history: v5's parent v3 and v4's parent v1 are not
// loaded yet. Both edges keep their lanes, dashed, to the end of the list
// instead of stopping in their own row -- a stub that stops would sit right
// above v4's dot on the same lane and read as "v4 is v5's parent".
export const OlderPageNotLoaded = {
  render: () => (
    <ReaderColumn>
      <ContextHistory
        {...baseProps}
        detail={detail({
          current_version_id: "version-6",
          current_published_version_id: "version-6",
          visible_version_id: "version-6",
        })}
        history={{
          ...history([
            historyVersion("version-6", 6, "published_snapshot", {
              is_current_published: true,
              is_visible_head: true,
            }),
            historyVersion("version-5", 5, "superseded_snapshot", {
              prior_version_id: "version-3",
            }),
            historyVersion("version-4", 4, "alternative_draft", {
              prior_version_id: "version-1",
              is_alternative: true,
            }),
          ]),
          next_cursor: "older-page",
        }}
      />
    </ReaderColumn>
  ),
};

// Three states at once: a hidden parent ("Comparison unavailable"), a
// state_inconsistent row (danger tone rail), and reconciliation_available on
// the same row a discard is also valid for -- the case the action-row
// grouping (discard at the left edge, restore/reconcile/compare at the
// right) exists to keep from reading as a random button list.
export const States = {
  render: () => (
    <ReaderColumn>
      <ContextHistory
        {...baseProps}
        detail={detail({
          current_version_id: "version-3",
          current_published_version_id: "version-3",
          visible_version_id: "version-3",
        })}
        history={history([
          historyVersion("version-3", 3, "published_snapshot", {
            is_visible_head: true,
            is_current_published: true,
            prior_relation: "hidden",
          }),
          historyVersion("version-2", 2, "state_inconsistent"),
          historyVersion("version-1", 1, "alternative_draft", {
            is_alternative: true,
            reconciliation_available: true,
          }),
        ])}
      />
    </ReaderColumn>
  ),
};
