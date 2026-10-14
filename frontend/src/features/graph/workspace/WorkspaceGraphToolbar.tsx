import { Eye, Filter, Search } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import type { WorkspaceGraphAvailableLens, WorkspaceGraphScene } from "../../../shared/api/workspaceGraph";
import type { WorkspaceGraphRestorableState } from "../workspaceGraphModel";
import type { WorkspaceMapDeviceProfile, WorkspaceMapScale } from "../shared/workspaceMapBudget";

export function visibleWorkspaceGraphDocumentCount(scene: WorkspaceGraphScene, nodeIds: readonly string[]): number {
  const visible = new Set(nodeIds);
  return scene.nodes.reduce((count, node) => count + (visible.has(node.id)
    ? node.grain === "cluster" ? node.member_count : 1 : 0), 0);
}

/** Immediate view controls stay separate from Refine's staged data filters. */
export function WorkspaceGraphToolbar({
  activeFilterCount, display, folderAvailable, folderEnabled, onDisplay,
  onLens, onOpenRefine, onSearch, onToggleFolder, orphanCount, pendingChanges,
  ready, search, showOrphans, lens, mobileLayout, deviceProfile, mapScale, onMapScale,
}: {
  activeFilterCount: number;
  display: WorkspaceGraphRestorableState["display"];
  folderAvailable: boolean;
  folderEnabled: boolean;
  lens: WorkspaceGraphAvailableLens;
  mobileLayout: boolean;
  deviceProfile: WorkspaceMapDeviceProfile;
  mapScale: WorkspaceMapScale;
  onMapScale(scale: WorkspaceMapScale): void;
  onDisplay(display: WorkspaceGraphRestorableState["display"]): void;
  onLens(lens: WorkspaceGraphAvailableLens): void;
  onOpenRefine(trigger: HTMLButtonElement): void;
  onSearch(search: string): void;
  onToggleFolder(): void;
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
          <option disabled value="growth">{t("workspaceGraph.controls.growth")}</option>
          <option disabled value="provenance">{t("workspaceGraph.controls.provenance")}</option>
        </select>
      </label>
      <div className="workspace-graph-visible-search">
        <Search aria-hidden="true" size={16} />
        <label className="visually-hidden" htmlFor="workspace-graph-visible-search">{t("workspaceGraph.ui.findVisible")}</label>
        <input disabled={!ready} id="workspace-graph-visible-search" onChange={(event) => onSearch(event.currentTarget.value)}
          placeholder={t(mobileLayout ? "workspaceGraph.ui.findVisiblePlaceholderMobile"
            : "workspaceGraph.ui.findVisiblePlaceholder")} type="search" value={search} />
      </div>
      <button aria-describedby={refineStatus ? "workspace-graph-refine-status" : undefined}
        aria-label={t("workspaceGraph.controls.refine")} className="workspace-graph-refine-trigger"
        onClick={(event) => {
          if (viewRef.current) viewRef.current.open = false;
          onOpenRefine(event.currentTarget);
        }} type="button">
        <Filter aria-hidden="true" size={15} /><span className="workspace-graph-toolbar-text">{t("workspaceGraph.controls.refine")}</span>
        {activeFilterCount ? <span aria-hidden="true" className="workspace-graph-refine-state">{t("workspaceGraph.controls.activeFilterCount", { count: activeFilterCount })}</span> : null}
        {pendingChanges ? <span aria-hidden="true" className="workspace-graph-refine-state is-pending">{t("workspaceGraph.controls.pendingChanges")}</span> : null}
      </button>
      {refineStatus ? <span className="visually-hidden" id="workspace-graph-refine-status">{refineStatus}</span> : null}
      <details className="workspace-graph-view-options" ref={viewRef}>
        <summary><Eye aria-hidden="true" size={16} />{t("workspaceGraph.ui.view")}</summary>
        <div className="workspace-graph-view-panel">
          {deviceProfile === "desktop" ? <label>
            {t("workspaceGraph.ui.mapSize")}
            <select onChange={(event) => onMapScale(event.currentTarget.value as WorkspaceMapScale)} value={mapScale}>
              <option value="2k">{t("workspaceGraph.ui.mapSize2k")}</option>
              <option value="10k">{t("workspaceGraph.ui.mapSize10k")}</option>
            </select>
          </label> : <small>{t("workspaceGraph.ui.mobileMapLimit")}</small>}
          <button aria-pressed={folderEnabled} className="workspace-graph-folder-toggle" disabled={!folderAvailable}
            onClick={onToggleFolder} type="button">{t("workspaceGraph.folders.groupByFolder")}</button>
          <small>{t(folderAvailable ? "workspaceGraph.folders.primaryOnly" : "workspaceGraph.folders.unavailable")}</small>
          <label><input checked={display.show_edges} onChange={(event) => onDisplay({ ...display, show_edges: event.currentTarget.checked })}
            type="checkbox" />{t("workspaceGraph.drawer.showRelationships")}</label>
          <label><input checked={display.show_labels} onChange={(event) => onDisplay({ ...display, show_labels: event.currentTarget.checked })}
            type="checkbox" />{t("workspaceGraph.drawer.showLabels")}</label>
        </div>
      </details>
      {orphanCount ? <button aria-pressed={showOrphans} className="workspace-graph-orphan-toggle"
        aria-label={t(showOrphans ? "workspaceGraph.ui.hideOrphans" : "workspaceGraph.ui.showOrphans")}
        onClick={() => onDisplay({ ...display, show_orphans: !showOrphans })}
        title={t(showOrphans ? "workspaceGraph.ui.orphansShown" : "workspaceGraph.ui.orphansHidden", { count: orphanCount })}
        type="button">{t("workspaceGraph.ui.unlinkedCount", { count: orphanCount })}</button> : null}
    </section>
  );
}
