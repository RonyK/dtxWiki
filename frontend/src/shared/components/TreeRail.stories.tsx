import type { TreeNodePayload } from "../api/workspace";
import { TreeRail } from "./TreeRail";

const storyNow = Date.now();
const title = { context: "Context", library: "Library", agent: "Agent" };

function markerNodes(section: "context" | "library" | "agent"): TreeNodePayload[] {
  const kind = section === "context" ? "context_page" : section === "library" ? "library_item" : "agent_page";
  return (["normal", "recent", "review"] as const).flatMap((state, index) =>
    [false, true].map((favorite) => ({
      id: `${section}-${state}-${favorite}`, section,
      title: `${state === "normal" ? "Normal" : state === "recent" ? "Recent change" : "Pending review"}${favorite ? " · favorite" : ""}`,
      slug: null, node_kind: "page_ref", parent_id: null,
      sort_key: `${index}-${Number(favorite)}`, target_kind: kind,
      target_id: `${section}-${state}-${favorite}`, is_system_node: false,
      can_have_children: false, primary_placement: true, visibility_status: "visible",
      children_count: 0, breadcrumb: [], is_favorite: favorite,
      content_changed_at: new Date(storyNow - (state === "normal" ? 12 * 86_400_000 : 3_600_000)).toISOString(),
      review_status: state === "review" ? "pending" : null,
      review_requested_at: state === "review" ? new Date(storyNow - 2 * 86_400_000).toISOString() : null,
    })),
  );
}

const meta = {
  title: "Shared/TreeRail",
  component: TreeRail,
  args: { title: "Context", nodes: markerNodes("context"), accent: "context" },
};

export default meta;
export const DocumentMarkers = {
  render: () => <div style={{ display: "flex", flexWrap: "wrap", gap: 24 }}>
    {(["context", "library", "agent"] as const).map((section) => <div key={section} style={{ width: 280, height: 360 }}>
      <TreeRail title={title[section]} accent={section} nodes={markerNodes(section)} activeNodeId={`${section}-review-true`} />
    </div>)}
  </div>,
};

export const TooltipReadingAndScroll = {
  render: () => <div>
    <p>Hover a row, sweep down across its tooltip to the rows underneath, wheel over it, and press Escape. Focus a row and use PageDown to scroll it out of view.</p>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 24 }}>
      {(["ltr", "rtl"] as const).map((direction) => <div key={direction} dir={direction} style={{ width: 280, maxWidth: "100%", height: 300 }}>
        <TreeRail title={`Context · ${direction.toUpperCase()}`} accent="context" nodes={Array.from({ length: 30 }, (_, index) => ({
          ...markerNodes("context")[index % 6],
          id: `${direction}-${index}`,
          sort_key: index.toString().padStart(2, "0"),
          title: `${index + 1}. Full document title for reading status and favorite details in a scrollable tree`,
        }))} />
      </div>)}
    </div>
  </div>,
};
