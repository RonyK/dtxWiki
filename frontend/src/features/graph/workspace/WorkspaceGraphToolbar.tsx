import { Filter, Search } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { workspaceGraphNodeDocuments, type WorkspaceGraphAvailableLens, type WorkspaceGraphScene } from "../../../shared/api/workspaceGraph";
import type { WorkspaceGraphRestorableState } from "../workspaceGraphModel";
import type { WorkspaceMapDeviceProfile, WorkspaceMapScale } from "../shared/workspaceMapBudget";
import { GraphViewMenu } from "../shared/GraphViewMenu";

export function visibleWorkspaceGraphDocumentCount(scene: WorkspaceGraphScene, nodeIds: readonly string[]): number {
  const visible = new Set(nodeIds);
  return scene.nodes.reduce((count, node) => count + (visible.has(node.id) ? workspaceGraphNodeDocuments(node) : 0), 0);
}

/**
 * Documents drawn one by one versus documents standing inside clusters, so the
 * header never presents grouped documents as plain coverage. `nodeIds` limits
 * the count to the rows the Map actually holds; null when no cluster is in it.
 */
export function workspaceGraphClusterCoverage(
  scene: WorkspaceGraphScene,
  nodeIds?: readonly string[],
): { shown: number; grouped: number; clusters: number } | null {
  const visible = nodeIds && new Set(nodeIds);
  let shown = 0, grouped = 0, clusters = 0;
  for (const node of scene.nodes) {
    if (visible && !visible.has(node.id)) continue;
    if (node.grain === "cluster") { grouped += node.member_count; clusters += 1; } else shown += workspaceGraphNodeDocuments(node);
  }
  return clusters ? { shown, grouped, clusters } : null;
}

/** Immediate view controls stay separate from Refine's staged data filters. */
export function WorkspaceGraphToolbar({
  activeFilterCount, display, onDisplay,
  onLens, onOpenRefine, onSearch, orphanCount, pendingChanges,
  ready, search, showOrphans, lens, deviceProfile, mapScale, onMapScale,
}: {
  activeFilterCount: number;
  display: WorkspaceGraphRestorableState["display"];
  lens: WorkspaceGraphAvailableLens;
  deviceProfile: WorkspaceMapDeviceProfile;
  mapScale: WorkspaceMapScale;
  onMapScale(scale: WorkspaceMapScale): void;
  onDisplay(display: WorkspaceGraphRestorableState["display"]): void;
  onLens(lens: WorkspaceGraphAvailableLens): void;
  onOpenRefine(trigger: HTMLButtonElement): void;
  onSearch(search: string): void;
  orphanCount: number;
  pendingChanges: boolean;
  ready: boolean;
  search: string;
  showOrphans: boolean;
}) {
  const { t } = useTranslation("graph");
  const viewRef = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const view = viewRef.current;
    const closeOutside = (event: PointerEvent) => {
      if (view?.open && event.target instanceof Node && !view.contains(event.target)) view.open = false;
    };
    const closeEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !view?.open) return;
      event.preventDefault();
      event.stopPropagation();
      view.open = false;
      view.querySelector("summary")?.focus();
    };
    document.addEventListener("pointerdown", closeOutside);
    view?.addEventListener("keydown", closeEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      view?.removeEventListener("keydown", closeEscape);
    };
  }, []);
  const refineStatus = [
    activeFilterCount ? t("workspaceGraph.controls.activeFilterCount", { count: activeFilterCount }) : null,
    pendingChanges ? t("workspaceGraph.controls.pendingChanges") : null,
  ].filter(Boolean).join(". ");
  return (
    <section aria-label={t("workspaceGraph.controls.aria")} className="workspace-graph-controls workspace-graph-toolbar">
      <label className="workspace-graph-control">
        <span className="visually-hidden">{t("workspaceGraph.controls.lens")}</span>
        <select aria-label={t("workspaceGraph.controls.lens")}
          onChange={(event) => onLens(event.currentTarget.value as WorkspaceGraphAvailableLens)} value={lens}>
          <option value="combined">{t("workspaceGraph.controls.combined")}</option>
          <option value="links">{t("workspaceGraph.controls.links")}</option>
          <option value="evidence">{t("workspaceGraph.controls.evidence")}</option>
          <option value="directory">{t("workspaceGraph.controls.directory")}</option>
          <option value="compare">{t("workspaceGraph.controls.compare")}</option>
        </select>
      </label>
      <div className="workspace-graph-visible-search">
        <Search aria-hidden="true" size={16} />
        <label className="visually-hidden" htmlFor="workspace-graph-visible-search">{t("workspaceGraph.ui.findVisible")}</label>
        <input disabled={!ready} id="workspace-graph-visible-search" onChange={(event) => onSearch(event.currentTarget.value)}
          placeholder={t("relationList.searchPlaceholder")} type="search" value={search} />
      </div>
      <button aria-describedby={refineStatus ? "workspace-graph-refine-status" : undefined}
        aria-label={t("graph.filters.refine")} className="workspace-graph-refine-trigger"
        onClick={(event) => {
          if (viewRef.current) viewRef.current.open = false;
          onOpenRefine(event.currentTarget);
        }} type="button">
        <Filter aria-hidden="true" size={15} /><span className="workspace-graph-toolbar-text">{t("graph.filters.refine")}</span>
        {activeFilterCount ? <span aria-hidden="true" className="workspace-graph-refine-state">{t("workspaceGraph.controls.activeFilterCount", { count: activeFilterCount })}</span> : null}
        {pendingChanges ? <span aria-hidden="true" className="workspace-graph-refine-state is-pending">{t("workspaceGraph.controls.pendingChanges")}</span> : null}
      </button>
      {refineStatus ? <span className="visually-hidden" id="workspace-graph-refine-status">{refineStatus}</span> : null}
      <GraphViewMenu ref={viewRef}>
          {deviceProfile === "desktop" ? <label>
            {t("workspaceGraph.ui.mapSize")}
            <select onChange={(event) => onMapScale(event.currentTarget.value as WorkspaceMapScale)} value={mapScale}>
              <option value="2k">{t("workspaceGraph.ui.mapSize2k")}</option>
              <option value="10k">{t("workspaceGraph.ui.mapSize10k")}</option>
            </select>
          </label> : <small>{t("workspaceGraph.ui.mobileMapLimit")}</small>}
          <label><input checked={display.show_edges} onChange={(event) => onDisplay({ ...display, show_edges: event.currentTarget.checked })}
            type="checkbox" />{t("graph.display.relationships")}</label>
          <label><input checked={display.show_labels} onChange={(event) => onDisplay({ ...display, show_labels: event.currentTarget.checked })}
            type="checkbox" />{t("graph.display.labels")}</label>
      </GraphViewMenu>
      {orphanCount ? <button aria-pressed={showOrphans} className="workspace-graph-orphan-toggle"
        onClick={() => onDisplay({ ...display, show_orphans: !showOrphans })}
        title={t(showOrphans ? "workspaceGraph.ui.orphansShown" : "workspaceGraph.ui.orphansHidden", { count: orphanCount })}
        type="button">{t("workspaceGraph.ui.unlinkedCount", { count: orphanCount })}</button> : null}
    </section>
  );
}
