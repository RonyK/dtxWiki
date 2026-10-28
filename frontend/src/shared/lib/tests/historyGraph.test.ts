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
        !segment.merge &&
        segment.lane !== row.lane
      ) {
        problems.push(`I3 ${name}: its parent edge leaves outside its lane`);
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
    name: "fork merged back by a reconcile version",
    head: "v5",
    versions: [
      version(5, 4),
      version(4, 2),
      version(3, 1, { merged_into_version_id: "v5" }),
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
      version(3, null, { prior_relation: "hidden" }),
      version(2, 1, { merged_into_version_id: "v3" }),
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
