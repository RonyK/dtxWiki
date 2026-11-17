import type {
  WorkspaceGraphAvailableLens,
  WorkspaceGraphDataFilters,
} from "../../../shared/api/workspaceGraph";
import type { GraphViewport } from "../shared/rendererTypes";

export type WorkspaceGraphDisplaySettings = {
  show_edges: boolean;
  show_labels: boolean;
  show_orphans: boolean;
};

export type WorkspaceGraphRestorableState = {
  lens: WorkspaceGraphAvailableLens;
  dataFilters: WorkspaceGraphDataFilters;
  display: WorkspaceGraphDisplaySettings;
  selectedId: string | null;
  pinnedIds: string[];
  pinnedPositions: Record<string, { x: number; y: number }>;
  viewport: GraphViewport | null;
  /** Prevent old Cytoscape camera and another actor's transient state restoring into Cosmos. */
  viewportEngine?: "cosmos";
  viewerId?: string;
};

export const DEFAULT_WORKSPACE_GRAPH_FILTERS: WorkspaceGraphDataFilters = {
  search: "",
  document_kinds: ["context", "library", "agent"],
  time_basis: "document_updated",
  time_from: null,
  time_to: null,
  quick_filters: [],
  facet_rules: [],
};

export const DEFAULT_WORKSPACE_GRAPH_STATE: WorkspaceGraphRestorableState = {
  lens: "combined",
  dataFilters: DEFAULT_WORKSPACE_GRAPH_FILTERS,
  display: { show_edges: true, show_labels: true, show_orphans: false },
  selectedId: null,
  pinnedIds: [],
  pinnedPositions: {},
  viewport: null,
};

export function freshWorkspaceGraphState(): WorkspaceGraphRestorableState {
  return {
    ...DEFAULT_WORKSPACE_GRAPH_STATE,
    dataFilters: cloneWorkspaceGraphFilters(DEFAULT_WORKSPACE_GRAPH_FILTERS),
    display: { ...DEFAULT_WORKSPACE_GRAPH_STATE.display },
    pinnedIds: [],
    pinnedPositions: {},
  };
}

export function cloneWorkspaceGraphFilters(filters: WorkspaceGraphDataFilters): WorkspaceGraphDataFilters {
  return {
    ...filters,
    document_kinds: [...filters.document_kinds],
    quick_filters: [...filters.quick_filters],
    facet_rules: filters.facet_rules.map((rule) => ({ ...rule })),
  };
}
