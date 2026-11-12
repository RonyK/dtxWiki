import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { GraphLegendGroup } from "./GraphCues";

export type GraphLegendNodeRow = { cue: ReactNode; key: string; label: string };
export type GraphLegendLineRow = { cue: ReactNode; key: string; label: string };

/**
 * One legend for both graphs: node glyphs, line styles, then what node size
 * means. A view lists what it can draw; the Context graph adds its history
 * marks alongside the node and line groups.
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
      <GraphLegendGroup title={t("graph.legend.nodes")}>
        {nodes.map((node) => <li data-node-visual-kind={node.key} key={node.key}>{node.cue}{node.label}</li>)}
      </GraphLegendGroup>
      <GraphLegendGroup title={t("graph.legend.edges")}>
        {lines.map((line) => (
          <li data-edge-visual-kind={line.key} key={line.key}>
            {line.cue}
            <span>{line.label}</span>
          </li>
        ))}
      </GraphLegendGroup>
      {children}
      {size.length ? <p className="graph-legend-note" data-legend-size="">
        {t("graph.legend.size")}: {size.join(" · ")}
      </p> : null}
    </div>
  );
}
