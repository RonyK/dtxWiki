import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { GraphLegendGroup } from "./GraphCues";

export type GraphLegendNodeRow = { cue: ReactNode; key: string; label: string };
export type GraphLegendLineRow = { cue: ReactNode; key: string; label: string };

/**
 * One legend for both graphs: node glyphs with their size meaning, and line
 * styles with endpoint roles. A view lists what it can draw; Context adds history
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
      <GraphLegendGroup title={t("graph.legend.nodes")} footer={size.length ? (
        <p className="graph-legend-note graph-node-size-note" data-legend-size="">
          {t("graph.legend.size")}: {size.join(" · ")}
        </p>
      ) : undefined}>
        {nodes.map((node) => <li data-node-visual-kind={node.key} key={node.key}>{node.cue}{node.label}</li>)}
      </GraphLegendGroup>
      <GraphLegendGroup className="graph-legend-lines" title={t("graph.legend.edges")}>
        {lines.map((line) => (
          <li data-edge-visual-kind={line.key} key={line.key}>
            {line.cue}
            <span className="graph-legend-line-label">{line.label}</span>
            <small className="graph-legend-line-description">{t(`graph.legend.endpoints.${line.key}`)}</small>
          </li>
        ))}
      </GraphLegendGroup>
      {children}
    </div>
  );
}
