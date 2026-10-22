// HISTORY GRAPH CONTRACT -- read this before changing anything in the graph.
// Full analysis, the history of earlier wrong turns, and a git oracle script
// live in the dtxWiki page "261022-Context History Graph 오류 분석·수정 방향"
// (f0dde0fb-6176-45ee-9d37-bcf4905715a2).
//
// The graph must have the same topology `git log --graph --date-order` draws
// for the same parent links; column order and colours may differ. Check a
// change against git itself (the oracle script), never against this module's
// own earlier output or a Storybook screenshot.
//
//   I1 No false adjacency. A line in a row's NODE lane must belong to an edge
//      that starts or ends at that node. Another edge running through a dot
//      makes the dot read as that edge's parent or child.
//   I2 Lane exclusivity. One edge per lane at a row's top boundary. Edges may
//      share a lane at a row's bottom boundary only when all of them enter the
//      very next row's node (the `|/` join).
//   I3 Late bend. A child->parent edge leaves in the CHILD's lane, stays in
//      it, and changes lane only in the row directly above the parent. The
//      one early bend is a merge edge's departure (`|\`) right below the
//      merge node.
//   I4 Trunk. Lane 0 holds only the head lineage -- draft head, published
//      head, then first parents -- taken from page-level ids, never "the
//      first-born child".
//   I5 Pagination. Lanes are assigned newest-first (top-down), so loading
//      older rows never moves a row that is already drawn.
//   I6 Unseen parents. An edge to a parent that is not loaded yet runs to the
//      bottom of the list; a hidden/missing-parent stub ends INSIDE its row.
//      Neither may touch another version's node.
//
// Do not: route an edge down its PARENT's lane; let two different edges share
// a lane to save width or colours (sibling forks that coexist need separate
// lanes); solve gutter overflow by sharing lanes; stretch a bend over the
// whole row height; accept a test that only checks a path string's shape or
// that an element exists -- assert which lane each edge occupies per row.
//
// Known gap: the reconcile merge edge is recorded on the BRANCH row
// (`merged_into_version_id`), so the merge version learns about it only once
// that older row is loaded. Until the API also names merge parents on the
// merge version, loading an older page that brings in a merged branch can
// re-seat rows below the merge version (I5 holds for everything else).

export type HistoryGraphRelation =
  | "root"
  | "linear"
  | "branch"
  | "older"
  | "hidden"
  | "missing";

export type PriorRelation = "root" | "visible" | "hidden" | "missing";

// What the layout needs to know about one version. Every history surface maps
// its own entries onto this (Context: parent_version_id, Agent: the previous
// version_no, Library: previous_version_id), so the layout never depends on a
// domain type and a new surface only adds an adapter.
export type HistoryGraphNode<T> = {
  entry: T;
  id: string;
  versionNo: number;
  // The version this one was made from. With parentRelation "visible" an id
  // that no loaded node carries means the parent is older than every loaded
  // row: the edge then runs to the bottom of the list (I6).
  parentId: string | null;
  parentRelation: PriorRelation;
  // Reconcile merge: the newer version that folded this branch back in.
  mergedIntoId?: string | null;
  // The surface already knows this version is off the main line (an
  // alternative or discarded draft). Siblings of one parent count as branches
  // regardless.
  branched?: boolean;
};

// One row's share of the drawing, in the row's own coordinates. Every edge is
// cut at row boundaries, so a row only ever needs these four shapes.
export type HistoryGraphSegment =
  // Top boundary -> this row's node, always in the node's lane: an edge that
  // has to change lane to reach this node already did so in the row above.
  // `tintLane` is the lane the arriving edge travelled in, for its colour.
  | { kind: "in"; tintLane: number }
  // Top boundary -> bottom boundary, beside this row's node and never through
  // it. `to !== lane` only in the row directly above the edge's target, where
  // the edge bends into the target's lane at the bottom of the row.
  | { kind: "through"; lane: number; to: number; unresolved: boolean }
  // This row's node -> bottom boundary, travelling in `lane`. A first-parent
  // edge travels in the node's lane; a merge edge departs right below the node
  // into its own lane. Either bends at the bottom when `to !== lane`.
  | {
      kind: "out";
      lane: number;
      to: number;
      merge: boolean;
      unresolved: boolean;
    }
  // Hidden or missing parent: a short terminal mark that ends inside the row.
  | { kind: "stub" };

export type HistoryGraphRow<T> = {
  node: HistoryGraphNode<T>;
  entry: T;
  lane: number;
  parent: HistoryGraphNode<T> | null;
  relation: HistoryGraphRelation;
  segments: HistoryGraphSegment[];
};

export type HistoryGraph<T> = {
  rows: HistoryGraphRow<T>[];
  laneCount: number;
};

type Edge = {
  // null: the parent exists but is older than every loaded row (I6).
  target: string | null;
  lane: number;
  merge: boolean;
};

export function buildHistoryGraph<T>(
  // Newest first, exactly as the history APIs page them.
  nodes: HistoryGraphNode<T>[],
  options: {
    // Page-level head ids in priority order -- draft head, published head,
    // visible head. The first one that is loaded seeds the trunk (I4).
    headCandidateIds?: ReadonlyArray<string | null | undefined>;
    // False while older pages remain. A head that is not loaded may then
    // still be further down, so lane 0 stays reserved for it (I5).
    complete?: boolean;
  } = {},
): HistoryGraph<T> {
  const indexById = new Map(nodes.map((node, index) => [node.id, index]));
  const childCountByParent = new Map<string, number>();
  nodes.forEach((node) => {
    if (node.parentId) {
      childCountByParent.set(
        node.parentId,
        (childCountByParent.get(node.parentId) ?? 0) + 1,
      );
    }
  });
  // A parent is drawable only when it is loaded BELOW its child; anything
  // else would need an edge pointing up the newest-first list.
  const drawableParentIndex = (index: number): number | undefined => {
    const parentId = nodes[index].parentId;
    const parentIndex = parentId ? indexById.get(parentId) : undefined;
    return parentIndex !== undefined && parentIndex > index
      ? parentIndex
      : undefined;
  };

  const candidates = (options.headCandidateIds ?? []).filter(
    (id): id is string => Boolean(id),
  );
  const trunkTipIndex = candidates
    .map((id) => indexById.get(id))
    .find((index) => index !== undefined);
  const trunk = new Set<number>();
  for (
    let cursor = trunkTipIndex;
    cursor !== undefined;
    cursor = drawableParentIndex(cursor)
  ) {
    trunk.add(cursor);
  }
  // Lane 0 is kept free for the trunk wherever the trunk can still appear
  // below. With no head loaded yet, that is every loaded row.
  const reserveTrunkLaneThrough =
    trunk.size > 0
      ? Math.max(...trunk)
      : candidates.length > 0 && options.complete !== true
        ? Number.POSITIVE_INFINITY
        : -1;

  // The reconcile merge edge is recorded on the merged branch; the merge
  // version is the newer row, so it is the one the edge departs from.
  const mergedBranchesByIndex = new Map<number, string[]>();
  nodes.forEach((node, index) => {
    const mergeIndex = node.mergedIntoId
      ? indexById.get(node.mergedIntoId)
      : undefined;
    if (mergeIndex !== undefined && mergeIndex < index) {
      mergedBranchesByIndex.set(mergeIndex, [
        ...(mergedBranchesByIndex.get(mergeIndex) ?? []),
        node.id,
      ]);
    }
  });

  // Pass 1: lanes, top-down, the way `git log --graph` assigns columns. Each
  // lane holds the edge travelling down it, and an edge keeps the lane it
  // departed in until it reaches its target -- so siblings forked from one
  // version hold separate lanes all the way down to that version.
  const active: (Edge | null)[] = [];
  const laneOf: number[] = [];
  const topBoundary: (Edge | null)[][] = [];
  const departing: Edge[][] = [];
  const firstFreeLane = (index: number, exclude: number) => {
    let lane = index <= reserveTrunkLaneThrough ? 1 : 0;
    while (active[lane] || lane === exclude) {
      lane += 1;
    }
    return lane;
  };
  nodes.forEach((node, index) => {
    topBoundary.push(active.slice());
    const arriving = active.flatMap((edge, lane) =>
      edge?.target === node.id ? [lane] : [],
    );
    let lane = trunk.has(index)
      ? 0
      : arriving.length > 0
        ? Math.min(...arriving)
        : firstFreeLane(index, -1);
    if (active[lane] && active[lane]?.target !== node.id) {
      lane = firstFreeLane(index, -1);
    }
    laneOf.push(lane);
    arriving.forEach((arrivingLane) => {
      active[arrivingLane] = null;
    });
    const edges: Edge[] = [];
    if (node.parentId && node.parentRelation === "visible") {
      const parentLoaded = indexById.has(node.parentId);
      if (!parentLoaded || drawableParentIndex(index) !== undefined) {
        const edge: Edge = {
          target: parentLoaded ? node.parentId : null,
          lane,
          merge: false,
        };
        active[lane] = edge;
        edges.push(edge);
      }
    }
    for (const branchId of mergedBranchesByIndex.get(index) ?? []) {
      // Never the node's own lane: that slot belongs to the first-parent edge
      // or to a hidden-parent stub, and sharing it would hide the merge.
      const mergeLane = firstFreeLane(index, lane);
      const edge: Edge = { target: branchId, lane: mergeLane, merge: true };
      active[mergeLane] = edge;
      edges.push(edge);
    }
    departing.push(edges);
  });

  // Pass 2: cut every edge at row boundaries. The only lane change inside a
  // row happens at its bottom, and only when the next row is the target.
  const rows = nodes.map((node, index): HistoryGraphRow<T> => {
    const nextId = nodes[index + 1]?.id;
    const bottomLane = (edge: Edge) =>
      edge.target !== null && edge.target === nextId
        ? laneOf[index + 1]
        : edge.lane;
    const segments: HistoryGraphSegment[] = [];
    let inTintLane: number | null = null;
    topBoundary[index].forEach((edge, lane) => {
      if (!edge) return;
      if (edge.target === node.id) {
        if (inTintLane === null || lane === laneOf[index]) {
          inTintLane = lane;
        }
        return;
      }
      segments.push({
        kind: "through",
        lane,
        to: bottomLane(edge),
        unresolved: edge.target === null,
      });
    });
    if (inTintLane !== null) {
      segments.push({ kind: "in", tintLane: inTintLane });
    }
    departing[index].forEach((edge) => {
      segments.push({
        kind: "out",
        lane: edge.lane,
        to: bottomLane(edge),
        merge: edge.merge,
        unresolved: edge.target === null,
      });
    });

    const parentIndex = drawableParentIndex(index);
    const parent = parentIndex !== undefined ? nodes[parentIndex] : null;
    if (
      node.parentRelation === "hidden" ||
      node.parentRelation === "missing" ||
      (node.parentRelation === "visible" &&
        parent === null &&
        node.parentId &&
        indexById.has(node.parentId))
    ) {
      segments.push({ kind: "stub" });
    }
    const branch =
      Boolean(node.parentId) &&
      ((childCountByParent.get(node.parentId ?? "") ?? 0) > 1 ||
        node.branched === true);
    return {
      node,
      entry: node.entry,
      lane: laneOf[index],
      parent,
      relation:
        node.parentRelation === "root"
          ? "root"
          : node.parentRelation === "hidden"
            ? "hidden"
            : node.parentRelation === "missing"
              ? "missing"
              : !parent
                ? "older"
                : branch
                  ? "branch"
                  : "linear",
      segments,
    };
  });

  const usedLanes = rows.flatMap((row) => [
    row.lane,
    ...row.segments.flatMap((segment) =>
      segment.kind === "through" || segment.kind === "out"
        ? [segment.lane, segment.to]
        : [],
    ),
  ]);
  return { rows, laneCount: Math.max(1, ...usedLanes.map((lane) => lane + 1)) };
}
