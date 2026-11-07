import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";

import { RelationListChip, RelationListOpenLink } from "./parts";
import type { RelationListEdgeView, RelationListNodeView } from "./types";
import type { RovingSelectionOrigin } from "./useRovingList";

/** A selected relation: the same top-to-bottom reading as the result rows. */
export function RelationEnds({ edge, nodeView, onNavigate, onSelectEnd, roles, rowAttributes }: {
  edge: RelationListEdgeView;
  nodeView: (id: string) => RelationListNodeView | undefined;
  onNavigate?: (event: ReactMouseEvent<HTMLAnchorElement>) => void;
  /** When given, each end is a button that selects that end. */
  onSelectEnd?: (id: string, origin: RovingSelectionOrigin) => void;
  roles: { source: string; target: string };
  rowAttributes?: (id: string) => Record<string, string>;
}) {
  const end = (id: string, role: string) => {
    const node = nodeView(id);
    const body: ReactNode = <>
      <span className="relation-list-flow-name">
        {node?.cue ?? null}
        <strong>{node?.label ?? "—"}</strong>
        {node?.badge ?? null}
      </span>
      {node ? <span className="relation-list-row-meta"><RelationListChip chip={node.chip} />
        {node.meta ? <span className="relation-list-row-meta-text">{node.meta}</span> : null}
      </span> : null}
    </>;
    return <div className="relation-list-end-row">
      <span className="relation-list-end-role">{role}</span>
      {onSelectEnd && node ? <button className="relation-list-row-select relation-list-end-select" type="button"
        {...rowAttributes?.(id)}
        onClick={(event) => onSelectEnd(id, {
          control: event.currentTarget,
          modality: event.detail === 0 ? "keyboard" : "pointer",
        })}>{body}</button> : <span className="relation-list-end-body">{body}</span>}
      {node?.href ? <RelationListOpenLink href={node.href} label={node.label} onNavigate={onNavigate} /> : null}
    </div>;
  };
  return <div className="relation-list-ends">
    {end(edge.source, roles.source)}
    <div className="relation-list-ends-stroke">
      <span aria-hidden="true" className="relation-list-map-stroke" data-directed={edge.directed} data-visual={edge.stroke} />
      <RelationListChip chip={edge.chip} />
      {edge.note ? <span className="relation-list-ends-note">{edge.note}</span> : null}
    </div>
    {end(edge.target, roles.target)}
  </div>;
}
