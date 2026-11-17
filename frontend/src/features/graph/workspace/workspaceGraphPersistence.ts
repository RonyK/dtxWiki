import type {
  WorkspaceGraphAvailableLens,
  WorkspaceGraphDataFilters,
  WorkspaceGraphDocumentKind,
  WorkspaceGraphFacetRule,
} from "../../../shared/api/workspaceGraph";
import {
  clearLegacyWorkspaceGraphStorage,
  clearWorkspaceGraphStorage,
  workspaceGraphStorageScope,
} from "../../../shared/lib/workspaceGraphStorage";
import type { GraphViewport } from "../shared/rendererTypes";
import type { WorkspaceMapScale } from "../shared/workspaceMapBudget";
import {
  DEFAULT_WORKSPACE_GRAPH_STATE,
  freshWorkspaceGraphState,
  type WorkspaceGraphRestorableState,
} from "./workspaceGraphState";

/**
 * Camera and pins are coordinates of one scale's layout: the 2K CPU worker and
 * the 10K native GPU solver place a document at unrelated points. They persist
 * per scale and are never applied to the other one; selection stays shared.
 */
type WorkspaceGraphScaleView = Pick<WorkspaceGraphRestorableState, "viewport" | "pinnedIds" | "pinnedPositions">;
type VersionedWorkspaceGraphScaleView = WorkspaceGraphScaleView & { coordinateVersion: number };
type WorkspaceGraphScaleViews = Partial<Record<WorkspaceMapScale, VersionedWorkspaceGraphScaleView>>;
const WORKSPACE_MAP_SCALES: readonly WorkspaceMapScale[] = ["2k", "10k"];
// Coordinate compatibility belongs to the selected layout, not the renderer
// or backend graph revision. D3 replaces the previous 2K coordinate frame;
// native 10K keeps its existing frame, including records without a version.
const COORDINATE_VERSIONS: Readonly<Record<WorkspaceMapScale, number>> = { "2k": 2, "10k": 1 };
// Records written before scale keying carry no scale. 2K was the only layout
// then; a wrong guess would put a 2K camera on the 10K scene again.
const LEGACY_VIEW_SCALE: WorkspaceMapScale = "2k";
// Cosmos accepts zooms down to 1e-3. Preserve valid wide-layout fits below the
// legacy 0.05 storage floor rather than restoring them too close.
const MIN_VIEWPORT_ZOOM = 0.001;

/**
 * With `mapScale`, camera and pins come only from the view that scale captured;
 * the other scale's view stays parked in the record. Without it, the record's
 * top-level view is returned. Incompatible coordinate versions clear only
 * camera and pins, preserving scale-neutral settings and selection.
 */
export function loadWorkspaceGraphState(
  workspaceId: string,
  actorId: string,
  revision?: string | null,
  onInvalidOwnerRestoration?: () => void,
  mapScale?: WorkspaceMapScale,
): WorkspaceGraphRestorableState {
  if (typeof window === "undefined" || !workspaceId || !actorId) return freshWorkspaceGraphState();
  clearLegacyWorkspaceGraphStorage();
  try {
    const exactKey = storageKey(workspaceId, actorId, revision || "latest");
    const stored = window.sessionStorage.getItem(exactKey);
    if (!stored) return freshWorkspaceGraphState();
    const parsed = JSON.parse(stored) as StoredWorkspaceGraphState;
    const normalized = normalizeStoredState(parsed.state ?? parsed);
    if (normalized.viewerId !== actorId) {
      clearWorkspaceGraphState(workspaceId, actorId);
      onInvalidOwnerRestoration?.();
      return freshWorkspaceGraphState();
    }
    if (revision && parsed.revision === revision) {
      const scale = mapScale ?? normalizeScale(parsed.mapScale) ?? LEGACY_VIEW_SCALE;
      return { ...normalized, ...storedScaleView(parsed, normalized, scale) };
    }
    return {
      ...normalized,
      selectedId: null,
      pinnedIds: [],
      pinnedPositions: {},
      viewport: null,
    };
  } catch {
    return freshWorkspaceGraphState();
  }
}

/** Store camera and pins with their scale's coordinate version; default to 2K. */
export function saveWorkspaceGraphState(
  workspaceId: string,
  actorId: string,
  revision: string,
  state: WorkspaceGraphRestorableState,
  mapScale?: WorkspaceMapScale,
) {
  if (typeof window === "undefined" || !workspaceId || !actorId || (state.viewerId && state.viewerId !== actorId)) return;
  try {
    const serialized = JSON.stringify({
      revision,
      state: { ...state, viewerId: actorId },
      ...scaleFields(readStoredRecord(workspaceId, actorId, revision), mapScale ?? LEGACY_VIEW_SCALE),
    });
    window.sessionStorage.setItem(storageKey(workspaceId, actorId, revision), serialized);
    window.sessionStorage.setItem(storageKey(workspaceId, actorId, "latest"), serialized);
  } catch {
    // Persistence is optional when browser storage is unavailable/full.
  }
}

export function clearWorkspaceGraphState(workspaceId: string, actorId: string) {
  clearLegacyWorkspaceGraphStorage();
  clearWorkspaceGraphStorage(workspaceGraphStorageScope(workspaceId, actorId));
}

type StoredWorkspaceGraphState = Partial<WorkspaceGraphRestorableState> & {
  revision?: string;
  state?: Partial<WorkspaceGraphRestorableState>;
  /** Scale that owns the record's own camera and pins. */
  mapScale?: unknown;
  coordinateVersion?: unknown;
  /** The other scale's camera and pins, kept so a visit cannot erase them. */
  scaleViews?: unknown;
};

function emptyScaleView(): WorkspaceGraphScaleView {
  return { viewport: null, pinnedIds: [], pinnedPositions: {} };
}

function ownScaleView(state: WorkspaceGraphScaleView): WorkspaceGraphScaleView {
  return { viewport: state.viewport, pinnedIds: state.pinnedIds, pinnedPositions: state.pinnedPositions };
}

function normalizeScale(value: unknown): WorkspaceMapScale | null {
  return WORKSPACE_MAP_SCALES.find((scale) => scale === value) ?? null;
}

function normalizeScaleView(value: Record<string, unknown>): WorkspaceGraphScaleView {
  const pinnedIds = uniqueStrings(value.pinnedIds, isGraphId).slice(0, 250);
  return {
    viewport: normalizeViewport(value.viewport),
    pinnedIds,
    pinnedPositions: normalizePinnedPositions(value.pinnedPositions, pinnedIds),
  };
}

function normalizeScaleViews(value: unknown): WorkspaceGraphScaleViews {
  const views: WorkspaceGraphScaleViews = {};
  if (!isRecord(value)) return views;
  for (const scale of WORKSPACE_MAP_SCALES) {
    const view = value[scale];
    if (isRecord(view) && compatibleCoordinateVersion(view, scale)) {
      views[scale] = { ...normalizeScaleView(view), coordinateVersion: COORDINATE_VERSIONS[scale] };
    }
  }
  return views;
}

function compatibleCoordinateVersion(value: { coordinateVersion?: unknown }, scale: WorkspaceMapScale): boolean {
  return value.coordinateVersion === COORDINATE_VERSIONS[scale]
    || scale === "10k" && value.coordinateVersion === undefined;
}

function storedScaleView(
  record: StoredWorkspaceGraphState,
  state: WorkspaceGraphRestorableState,
  scale: WorkspaceMapScale,
): WorkspaceGraphScaleView {
  if ((normalizeScale(record.mapScale) ?? LEGACY_VIEW_SCALE) === scale) {
    return compatibleCoordinateVersion(record, scale) ? ownScaleView(state) : emptyScaleView();
  }
  const parked = normalizeScaleViews(record.scaleViews)[scale];
  return parked ? ownScaleView(parked) : emptyScaleView();
}

/** The same-revision record this actor already saved, or null when none is trustworthy. */
function readStoredRecord(workspaceId: string, actorId: string, revision: string) {
  try {
    const stored = window.sessionStorage.getItem(storageKey(workspaceId, actorId, revision));
    if (!stored) return null;
    const record = JSON.parse(stored) as StoredWorkspaceGraphState;
    const state = normalizeStoredState(record.state ?? record);
    return record.revision === revision && state.viewerId === actorId ? { record, state } : null;
  } catch {
    // A damaged record is overwritten by the save that asked for it.
    return null;
  }
}

/** Tag the record's own view with its scale and park the other scale's view beside it. */
function scaleFields(
  previous: ReturnType<typeof readStoredRecord>,
  scale: WorkspaceMapScale,
): { mapScale: WorkspaceMapScale; coordinateVersion: number; scaleViews?: WorkspaceGraphScaleViews } {
  const parked = previous ? normalizeScaleViews(previous.record.scaleViews) : {};
  const previousScale = normalizeScale(previous?.record.mapScale) ?? LEGACY_VIEW_SCALE;
  if (previous && previousScale !== scale) {
    const view = storedScaleView(previous.record, previous.state, previousScale);
    if (view.viewport || view.pinnedIds.length) {
      parked[previousScale] = { ...view, coordinateVersion: COORDINATE_VERSIONS[previousScale] };
    }
  }
  delete parked[scale];
  return { mapScale: scale, coordinateVersion: COORDINATE_VERSIONS[scale],
    ...(Object.keys(parked).length ? { scaleViews: parked } : {}) };
}

function normalizeStoredState(
  value: unknown,
): WorkspaceGraphRestorableState {
  const state = isRecord(value) ? value : {};
  const allowedLenses: WorkspaceGraphAvailableLens[] = ["combined", "links", "evidence", "directory", "compare"];
  const lens = typeof state.lens === "string"
    && allowedLenses.includes(state.lens as WorkspaceGraphAvailableLens)
    ? state.lens as WorkspaceGraphAvailableLens
    : DEFAULT_WORKSPACE_GRAPH_STATE.lens;
  const storedFilters = isRecord(state.dataFilters) ? state.dataFilters : {};
  const documentKinds = uniqueStrings(storedFilters.document_kinds, isDocumentKind);
  const quickFilters = uniqueStrings(storedFilters.quick_filters, isQuickFilter);
  const facetRules = normalizeFacetRules(storedFilters.facet_rules);
  const from = normalizeTimestamp(storedFilters.time_from);
  const to = normalizeTimestamp(storedFilters.time_to);
  const validWindow = from === null || to === null || from <= to;
  const { viewport, pinnedIds, pinnedPositions } = normalizeScaleView(state);
  return {
    lens,
    dataFilters: {
      search: typeof storedFilters.search === "string"
        ? storedFilters.search.slice(0, 200)
        : "",
      document_kinds: documentKinds.length ? documentKinds : ["context", "library", "agent"],
      time_basis: storedFilters.time_basis === "relation_observed"
        ? "relation_observed"
        : "document_updated",
      time_from: validWindow ? from : null,
      time_to: validWindow ? to : null,
      quick_filters: quickFilters,
      facet_rules: facetRules,
    },
    display: {
      show_edges: !isRecord(state.display) || state.display.show_edges !== false,
      show_labels: !isRecord(state.display) || state.display.show_labels !== false,
      show_orphans: isRecord(state.display) && state.display.show_orphans === true,
    },
    selectedId: isGraphId(state.selectedId) ? state.selectedId : null,
    pinnedIds,
    pinnedPositions,
    viewport,
    ...(state.viewportEngine === "cosmos" ? { viewportEngine: "cosmos" as const } : {}),
    ...(typeof state.viewerId === "string" && state.viewerId.length > 0 && state.viewerId.length <= 128
      ? { viewerId: state.viewerId } : {}),
  };
}

function isDocumentKind(value: unknown): value is WorkspaceGraphDocumentKind {
  return value === "context" || value === "library" || value === "agent";
}

function isQuickFilter(value: unknown): value is WorkspaceGraphDataFilters["quick_filters"][number] {
  return value === "orphans"
    || value === "highly_referenced"
    || value === "cross_folder"
    || value === "recently_connected";
}

function isGraphId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 220;
}

function normalizeFacetRules(value: unknown): WorkspaceGraphFacetRule[] {
  if (!Array.isArray(value)) return [];
  const rules: WorkspaceGraphFacetRule[] = [];
  const seen = new Set<string>();
  for (const candidate of value) {
    if (!isRecord(candidate)
      || (candidate.facet !== "folder" && candidate.facet !== "document_kind")
      || (candidate.mode !== "include" && candidate.mode !== "exclude" && candidate.mode !== "only")
      || typeof candidate.value !== "string") continue;
    if (candidate.facet === "folder" && candidate.value !== "unplaced" && !isUuid(candidate.value)) continue;
    if (candidate.facet === "document_kind" && !isDocumentKind(candidate.value)) continue;
    const key = `${candidate.facet}:${candidate.mode}:${candidate.value}`;
    if (seen.has(key)) continue;
    seen.add(key);
    rules.push({
      facet: candidate.facet,
      mode: candidate.mode,
      value: candidate.value,
    });
    if (rules.length === 64) break;
  }
  const folderOnly = rules.find((rule) => rule.facet === "folder" && rule.mode === "only");
  return folderOnly
    ? [folderOnly, ...rules.filter((rule) => rule.facet !== "folder")]
    : rules;
}

function normalizeTimestamp(value: unknown) {
  if (typeof value !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null;
}

function normalizePinnedPositions(
  value: unknown,
  pinnedIds: string[],
): Record<string, { x: number; y: number }> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(pinnedIds.flatMap((id) => {
    const point = value[id];
    return isPoint(point) ? [[id, point]] : [];
  }));
}

function normalizeViewport(value: unknown): GraphViewport | null {
  if (!isRecord(value) || !isPoint(value.pan) || !isFiniteNumber(value.zoom) || value.zoom <= 0) {
    return null;
  }
  return {
    zoom: Math.min(10, Math.max(MIN_VIEWPORT_ZOOM, value.zoom)),
    pan: value.pan,
  };
}

function isPoint(value: unknown): value is { x: number; y: number } {
  return isRecord(value) && isFiniteNumber(value.x) && isFiniteNumber(value.y);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isUuid(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function uniqueStrings<T extends string>(
  value: unknown,
  predicate: (candidate: unknown) => candidate is T,
): T[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter(predicate))];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function storageKey(workspaceId: string, actorId: string, revision: string) {
  return `${workspaceGraphStorageScope(workspaceId, actorId)}${encodeURIComponent(revision)}`;
}
