import { describe, expect, it } from "vitest";

import type { AgentVersionEntry } from "../../api/agent";
import type { VersionEntry } from "../../api/context";
import type { LibraryItemDetail } from "../../api/library";
import { agentHistoryNodes } from "../../../features/agent/agentHistory";
import { libraryHistoryNodes } from "../../../features/library/libraryHistory";
import { contextHistoryNodes } from "../contextHistory";
import { buildHistoryGraph, type HistoryGraph } from "../historyGraph";

function version(
  versionNo: number,
  prior: number | null,
  extra: Partial<VersionEntry> = {},
): VersionEntry {
  return {
    version_id: `v${versionNo}`,
    version_no: versionNo,
    version_status: "published_snapshot",
    actor_type: "human",
    prior_version_id: prior ? `v${prior}` : null,
    prior_relation: prior ? "visible" : "root",
    ...extra,
  } as VersionEntry;
}

// Follow an edge that leaves its row outside the node's lane down to the dot
// it enters; that version must be connected to the row it left.
function departureLanding(
  graph: HistoryGraph<unknown>,
  index: number,
  lane: number,
) {
  for (const row of graph.rows.slice(index + 1)) {
    if (row.lane === lane) {
      return row.segments.some((segment) => segment.kind === "in")
        ? row
        : undefined;
    }
    const through = row.segments.find(
      (segment) => segment.kind === "through" && segment.lane === lane,
    );
    if (!through || through.kind !== "through") {
      return undefined;
    }
    lane = through.to;
  }
  return undefined;
}

// The contract in historyGraph.ts, checked on the segments a row draws. A lane
// table alone cannot tell the bug this guards against from the fix: in the
// same-base fork both put v5 on lane 1 and v4 on lane 0, and only the routing
// of v5's edge -- through v4's dot or beside it -- differs.
function contractViolations(
  graph: HistoryGraph<unknown>,
  trunkIds: string[],
) {
  const problems: string[] = [];
  graph.rows.forEach((row, index) => {
    const name = `v${row.node.versionNo}`;
    const next = graph.rows[index + 1];
    const edges = row.segments.flatMap((segment) =>
      segment.kind === "through" || segment.kind === "out" ? [segment] : [],
    );
    const entering = edges.flatMap((segment) =>
      segment.kind === "through" ? [segment.lane] : [],
    );
    if (entering.includes(row.lane)) {
      problems.push(`I1 ${name}: another edge runs through its dot`);
    }
    if (new Set(entering).size !== entering.length) {
      problems.push(`I2 ${name}: two edges share a lane entering the row`);
    }
    for (const segment of edges) {
      const shared =
        edges.filter((other) => other.to === segment.to).length > 1;
      const lands =
        next !== undefined &&
        segment.to === next.lane &&
        next.segments.some((other) => other.kind === "in");
      if ((shared || segment.to !== segment.lane) && !lands) {
        problems.push(
          `I2/I3 ${name}: an edge bends or shares lane ${segment.to} without entering the next row's node`,
        );
      }
      if (
        segment.kind === "out" &&
        segment.lane !== row.lane &&
        !segment.unresolved
      ) {
        // A merge departs below its merge node, or from a merge parent
        // listed above its child; a parent edge departs only from above.
        const landing = departureLanding(graph, index, segment.to);
        const connected =
          landing !== undefined &&
          (segment.merge
            ? (row.node.mergeParentIds ?? []).includes(landing.node.id) ||
              (landing.node.mergeParentIds ?? []).includes(row.node.id)
            : landing.parent?.id === row.node.id);
        if (!connected) {
          problems.push(`I3 ${name}: an edge leaves its lane for no connected version`);
        }
      }
    }
    if (
      row.segments.some((segment) => segment.kind === "stub") &&
      edges.some(
        (segment) => segment.kind === "out" && segment.lane === row.lane,
      )
    ) {
      problems.push(`I6 ${name}: a stub shares its lane with an edge`);
    }
    if (row.lane === 0 && !trunkIds.includes(row.node.id)) {
      problems.push(`I4 ${name}: off-trunk version on lane 0`);
    }
  });
  return problems;
}

// Expected lanes are the dtxWiki page's "target" pictures, each checked
// against `git log --graph --date-order` for the same parent links.
const FIXTURES: {
  name: string;
  head: string;
  versions: VersionEntry[];
  lanes: Record<string, number>;
}[] = [
  {
    name: "two drafts forked from the same version",
    head: "v4",
    versions: [
      version(5, 3, { history_status: "alternative_draft" }),
      version(4, 3, { history_status: "current_draft" }),
      version(3, 2),
      version(2, 1),
      version(1, null),
    ],
    lanes: { v5: 1, v4: 0, v3: 0, v2: 0, v1: 0 },
  },
  {
    name: "two chains diverged from v1",
    head: "v4",
    versions: [
      version(5, 3),
      version(4, 2),
      version(3, 1),
      version(2, 1),
      version(1, null),
    ],
    lanes: { v5: 1, v4: 0, v3: 1, v2: 0, v1: 0 },
  },
  {
    name: "three-way fork",
    head: "v4",
    versions: [version(4, 1), version(3, 1), version(2, 1), version(1, null)],
    lanes: { v4: 0, v3: 1, v2: 2, v1: 0 },
  },
  {
    name: "first-born child discarded, second child published",
    head: "v5",
    versions: [
      version(5, 3),
      version(4, 3, { history_status: "discarded_draft" }),
      version(3, 2),
      version(2, 1),
      version(1, null),
    ],
    lanes: { v5: 0, v4: 1, v3: 0, v2: 0, v1: 0 },
  },
  {
    name: "fork merged back by a merge version",
    head: "v5",
    versions: [
      version(5, 4, {
        parent_version_ids: ["v4", "v3"],
        history_merge_parent_version_ids: ["v3"],
      }),
      version(4, 2),
      version(3, 1),
      version(2, 1),
      version(1, null),
    ],
    lanes: { v5: 0, v4: 0, v3: 1, v2: 0, v1: 0 },
  },
  {
    name: "long-lived branch beside a trunk that keeps going",
    head: "v6",
    versions: [
      version(6, 5),
      version(5, 3),
      version(4, 1),
      version(3, 2),
      version(2, 1),
      version(1, null),
    ],
    lanes: { v6: 0, v5: 0, v4: 1, v3: 0, v2: 0, v1: 0 },
  },
  {
    // A merge leaving a hidden-parent version must not take the node's own
    // lane: that is where the stub sits, and the two would read as one line.
    name: "merge from a version whose parent is hidden",
    head: "v3",
    versions: [
      version(3, null, {
        prior_relation: "hidden",
        parent_version_ids: ["v2"],
        history_merge_parent_version_ids: ["v2"],
      }),
      version(2, 1),
      version(1, null),
    ],
    lanes: { v3: 0, v2: 1, v1: 1 },
  },
];

function trunkOf(versions: VersionEntry[], head: string) {
  const byId = new Map(versions.map((entry) => [entry.version_id, entry]));
  const trunk: string[] = [];
  for (
    let cursor = byId.get(head);
    cursor;
    cursor = byId.get(cursor.prior_version_id ?? "")
  ) {
    trunk.push(cursor.version_id);
  }
  return trunk;
}

describe("buildHistoryGraph", () => {
  it("draws every fork in its own lane and joins it only at its parent", () => {
    for (const fixture of FIXTURES) {
      const graph = buildHistoryGraph(contextHistoryNodes(fixture.versions), {
        headCandidateIds: [fixture.head],
        complete: true,
      });
      expect(
        Object.fromEntries(graph.rows.map((row) => [row.node.id, row.lane])),
        fixture.name,
      ).toEqual(fixture.lanes);
      expect(
        contractViolations(graph, trunkOf(fixture.versions, fixture.head)),
        fixture.name,
      ).toEqual([]);
    }
  });

  it("keeps already drawn rows in place when older pages load", () => {
    for (const fixture of FIXTURES) {
      const full = buildHistoryGraph(contextHistoryNodes(fixture.versions), {
        headCandidateIds: [fixture.head],
        complete: true,
      }).rows.map((row) => row.lane);
      for (let size = 1; size < fixture.versions.length; size += 1) {
        const page = buildHistoryGraph(
          contextHistoryNodes(fixture.versions.slice(0, size)),
          {
            headCandidateIds: [fixture.head],
            complete: false,
          },
        ).rows.map((row) => row.lane);
        expect(page, `${fixture.name}, first ${size}`).toEqual(
          full.slice(0, size),
        );
      }
    }
  });

  it("connects a published version to its source draft listed above it", () => {
    const nodes = contextHistoryNodes([
      version(2, 1, { version_status: "draft" }),
      version(4, 1),
      version(3, 2, {
        parent_version_id: "v1",
        parent_version_ids: ["v1"],
      }),
      version(1, null),
    ]);
    const options = { allowParentsAbove: true, headCandidateIds: ["v2"] };
    const before = buildHistoryGraph(nodes.slice(0, 2), options);
    const graph = buildHistoryGraph(nodes, { ...options, complete: true });
    const [draft, other, child] = graph.rows;

    expect(graph.rows.map((row) => row.node.id)).toEqual(["v2", "v4", "v3", "v1"]);
    expect(contractViolations(before, ["v2"])).toEqual([]);
    expect(contractViolations(graph, ["v2", "v1"])).toEqual([]);
    expect([draft.lane, other.lane]).toEqual(before.rows.map((row) => row.lane));
    expect(child.parent?.id).toBe("v2");
    expect(child.relation).toBe("linear");
    expect(child.segments).not.toContainEqual({ kind: "stub" });
    expect(draft.segments).toContainEqual({
      kind: "out",
      lane: 3,
      to: 3,
      merge: false,
      unresolved: false,
    });
    expect(other.segments).toContainEqual({
      kind: "through",
      lane: 3,
      to: child.lane,
      unresolved: false,
    });
    expect(child.segments).toContainEqual({ kind: "in", tintLane: 3 });

    // A draft that brought in the newer v6 and was then published as the same
    // version lists that merge parent above it.
    const published = buildHistoryGraph(
      contextHistoryNodes([
        version(6, 1),
        version(5, 1, { history_merge_parent_version_ids: ["v6"] }),
        version(1, null),
      ]),
      { allowParentsAbove: true, headCandidateIds: ["v6"], complete: true },
    );
    expect(published.rows[0].segments).toContainEqual(
      expect.objectContaining({ kind: "out", merge: true }),
    );
    expect(contractViolations(published, ["v6", "v1"])).toEqual([]);

    // 250 commits whose parents are the 250 drafts listed above them. Their
    // lineage ends at the drafts' root, so lane 0 is free below it (I4).
    const drafts = Array.from({ length: 250 }, (_, i) =>
      version(250 - i, 249 - i || null, { version_status: "draft" }),
    );
    const commits = Array.from({ length: 250 }, (_, i) => version(500 - i, 250 - i));
    const long = buildHistoryGraph(contextHistoryNodes([...drafts, ...commits]), {
      allowParentsAbove: true,
      headCandidateIds: ["v250"],
      complete: true,
    });
    expect(long.rows.slice(250).every((row) => row.parent !== null)).toBe(true);
    expect(
      contractViolations(long, []).filter((problem) => !problem.startsWith("I4")),
    ).toEqual([]);
  });

  it("derives Agent and Library lineage the History tab draws", () => {
    // Agent versions record no parent: the previous version_no is linked
    // only as the previous version, a gap inside the loaded run is a version
    // this reader cannot see, and a gap below the oldest loaded row is just
    // not loaded yet.
    const agent = (versionNo: number) =>
      ({ version_id: `a${versionNo}`, version_no: versionNo }) as AgentVersionEntry;
    const agentRows = buildHistoryGraph(
      agentHistoryNodes([agent(5), agent(3), agent(2)]),
      { complete: true },
    ).rows;
    expect(
      agentRows.map((row) => [row.node.id, row.relation, row.parent?.id ?? null]),
    ).toEqual([
      ["a5", "hidden", null],
      ["a3", "linear", "a2"],
      ["a2", "older", null],
    ]);
    expect(
      agentRows[2].segments.some(
        (segment) => segment.kind === "out" && segment.unresolved,
      ),
    ).toBe(true);
    expect(
      buildHistoryGraph(agentHistoryNodes([agent(1)])).rows[0].relation,
    ).toBe("root");

    // Library versions name the upload they replaced; the item detail does
    // not promise an order, so the adapter sorts newest first.
    const library = (id: string, versionNo: number, previous: string | null) =>
      ({
        id,
        version_no: versionNo,
        previous_version_id: previous,
      }) as LibraryItemDetail["versions"][number];
    const libraryRows = buildHistoryGraph(
      libraryHistoryNodes([library("l1", 1, null), library("l2", 2, "l1")]),
      { headCandidateIds: ["l2"], complete: true },
    ).rows;
    expect(
      libraryRows.map((row) => [row.node.id, row.lane, row.relation]),
    ).toEqual([
      ["l2", 0, "linear"],
      ["l1", 0, "root"],
    ]);
  });
});
