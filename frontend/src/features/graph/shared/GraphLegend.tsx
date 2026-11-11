import type { TFunction } from "i18next";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { domainLabel } from "../../../shared/lib/domainLabel";

export type GraphLegendNodeRow = { cue: ReactNode; key: string; label: string };
export type GraphLegendLineRow = { cue: ReactNode; key: string; label: string; names?: string };

/**
 * The relationship names a line style draws, under the line's legend name.
 * A name equal to the legend name is not repeated.
 */
export function graphLegendRelationNames(edgeKinds: readonly string[], legendName: string, t: TFunction<"graph">): string {
  return [...new Set(edgeKinds.map((kind) => domainLabel("graph", "graphEdgeKind", kind, t)))]
    .filter((name) => name !== legendName).join(" · ");
}

/**
 * One legend for both graphs: node glyphs, line styles with the relationship
 * names each draws, then what node size means. A view lists what it can draw;
 * the Context graph adds its history marks after the size line.
 */
export function GraphLegendGroups({ children, lines, nodes, size }: {
  children?: ReactNode;
  lines: readonly GraphLegendLineRow[];
  nodes: readonly GraphLegendNodeRow[];
  /** What node size means in this view, in reading order. */
  size: readonly string[];
}) {
  const { t } = useTranslation("graph");
  return (
    <div className="graph-legend-groups">
      <ul aria-label={t("graph.legend.nodes")}>
        {nodes.map((node) => <li data-node-visual-kind={node.key} key={node.key}>{node.cue}{node.label}</li>)}
      </ul>
      <ul aria-label={t("graph.legend.edges")} className="graph-legend-stack">
        {lines.map((line) => (
          <li data-edge-visual-kind={line.key} key={line.key}>
            {line.cue}
            <span>{line.label}{line.names ? <small>{line.names}</small> : null}</span>
          </li>
        ))}
      </ul>
      {size.length ? <ul aria-label={t("graph.legend.size")} className="graph-legend-stack">
        <li data-legend-size="">{t("graph.legend.size")}: {size.join(" · ")}</li>
      </ul> : null}
      {children}
    </div>
  );
}
