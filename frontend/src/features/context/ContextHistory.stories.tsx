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

// Drives mergeToLane + passingLanes + incomingLanes + a branch curve at once
// -- a combination nothing else in the repo exercises. v1 -> v2 is the trunk;
// v3 branches off v1 onto its own lane (alternative_draft) and never grows
// its own descendants; the trunk keeps going v2 -> v4 (v4 "passes through"
// on the trunk lane, in between the branch's start and its merge); v5 is the
// reconcile-merge version, landing back on the trunk while its
// `merged_into_version_id` (set on v3, the branch) pulls the branch's lane
// back in. Getting the lineage backwards -- e.g. having v4/v5 continue FROM
// v3 instead of from v2 -- puts the branch and its merge target on the SAME
// lane, which made an earlier draft of this fixture render a degenerate,
// invisible "merge" (a curve from a lane back to itself).
export const ForkAndMerge = {
  render: () => (
    <ReaderColumn>
      <ContextHistory
        {...baseProps}
        detail={detail({ visible_version_id: "version-5" })}
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
        detail={detail({ visible_version_id: "version-3" })}
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
