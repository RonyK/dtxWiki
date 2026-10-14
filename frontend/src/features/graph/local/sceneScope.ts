import type { GraphFilters } from "../graphModel";
import type { GraphHistoryMode, GraphQueryRequest, GraphTimelineEvent } from "../../../shared/api/graph";

const orderedSet = (values: readonly string[] | undefined) => [...new Set(values ?? [])].sort();

/** Selection identity within an already authority-fenced Context owner. */
export function localSceneScopeKey({
  workspaceId, query, filters, historyMode, cursor, event,
}: {
  workspaceId: string | null;
  query: GraphQueryRequest;
  filters: GraphFilters;
  historyMode: GraphHistoryMode;
  cursor: number | null;
  event?: Pick<GraphTimelineEvent, "id" | "occurred_at" | "sequence">;
}): string {
  return JSON.stringify([
    workspaceId,
    // The first seed is the protected primary seed; ordering is meaningful.
    query.seeds.map(({ kind, id }) => [kind, id]),
    query.scope, query.preset, query.depth, query.direction,
    orderedSet(query.layers), orderedSet(query.node_families), orderedSet(query.node_kinds),
    orderedSet(query.edge_kinds), orderedSet(query.action_kinds),
    [query.time?.mode ?? "current", query.time?.as_of ?? null,
      query.time?.from ?? null, query.time?.to ?? null],
    [query.budget?.nodes ?? null, query.budget?.edges ?? null],
    historyMode,
    Object.entries(filters).sort(([left], [right]) => left.localeCompare(right))
      .map(([name, values]) => [name, orderedSet(values)]),
    // Current/latest has no selected event; watermarks and labels cannot
    // invalidate it. An explicit event includes its stable identity.
    [cursor, cursor === null ? null : event?.id ?? null,
      cursor === null ? null : event?.occurred_at ?? null,
      cursor === null ? null : event?.sequence ?? null],
  ]);
}
