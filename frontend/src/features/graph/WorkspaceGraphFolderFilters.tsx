import { X } from "lucide-react";
import { useEffect, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import type { WorkspaceGraphDataFilters, WorkspaceGraphFacetMode, WorkspaceGraphFacetOption } from "../../shared/api/workspaceGraph";
import { removeFacetRule, updateFacetRule } from "./workspaceGraphModel";

/** A local rule builder, not another server Apply or a fabricated folder tree. */
export function WorkspaceGraphFolderFilters({ filters, folders, onChange, searchRef }: {
  filters: WorkspaceGraphDataFilters;
  folders: readonly WorkspaceGraphFacetOption[];
  onChange: (filters: WorkspaceGraphDataFilters) => void;
  searchRef: RefObject<HTMLInputElement | null>;
}) {
  const { t } = useTranslation("graph");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState("");
  const [mode, setMode] = useState<WorkspaceGraphFacetMode>("include");
  const [notice, setNotice] = useState("");
  const rules = filters.facet_rules.filter((rule) => rule.facet === "folder");
  const available = new Map(folders.map((folder) => [folder.value, folder]));
  // A narrowed scene may omit a saved rule's facet. Keep its original value
  // editable/removable, without retaining old labels or inventing counts.
  const candidates = [...available.values(), ...rules
    .filter((rule) => !available.has(rule.value))
    .map((rule) => ({ value: rule.value, label: rule.value, count: null }))];
  const selectionAvailable = candidates.some((folder) => folder.value === selected);
  const selectedValue = selectionAvailable ? selected : "";
  useEffect(() => {
    // Retire a disappeared unadded choice, not merely its rendered value.
    // A later facet refresh must not resurrect an old editing intention.
    if (selected && !selectionAvailable) setSelected("");
  }, [selected, selectionAvailable]);
  const selectedRule = rules.find((rule) => rule.value === selectedValue);
  const term = search.trim().toLocaleLowerCase();
  const matchesSearch = (folder: { label: string; value: string }) => `${folder.label} ${folder.value}`.toLocaleLowerCase().includes(term);
  const matches = candidates.filter(matchesSearch);
  const options = candidates.filter((folder) => matchesSearch(folder) || folder.value === selectedValue);
  const labelCounts = new Map<string, number>();
  for (const folder of available.values()) labelCounts.set(folder.label, (labelCounts.get(folder.label) ?? 0) + 1);
  const labelFor = (value: string) => {
    const label = available.get(value)?.label;
    return label && (labelCounts.get(label) ?? 0) > 1 ? `${label} · ${value}` : label ?? value;
  };
  const modeLabel = (value: WorkspaceGraphFacetMode) => t(`workspaceGraph.drawer.${value}`);

  return (
    <div className="workspace-graph-folder-filter">
      <label>
        <span>{t("workspaceGraph.drawer.findFolder")}</span>
        <input onChange={(event) => setSearch(event.currentTarget.value)} ref={searchRef}
          type="search" value={search} />
      </label>
      <small>{t("workspaceGraph.drawer.folderSearchHint")}</small>
      <label>
        <span>{t("workspaceGraph.drawer.folder")}</span>
        <select disabled={!candidates.length} onChange={(event) => {
          const value = event.currentTarget.value;
          setSelected(value);
          setMode(rules.find((rule) => rule.value === value)?.mode ?? "include");
          setNotice("");
        }} value={selectedValue}>
          <option value="">{t("workspaceGraph.drawer.chooseFolder")}</option>
          {options.map((folder) => <option key={folder.value} value={folder.value}>
            {folder.count === null
              ? t("workspaceGraph.drawer.unavailableFolder", { label: labelFor(folder.value) })
              : `${labelFor(folder.value)} · ${t("workspaceGraph.drawer.visibleCandidates", { count: folder.count })}`}
          </option>)}
        </select>
      </label>
      {!candidates.length ? <p>{t("workspaceGraph.drawer.noFolders")}</p> : null}
      {term && !matches.length ? <p>{t("workspaceGraph.drawer.noFolderMatches")}</p> : null}
      <div className="workspace-graph-folder-rule-builder">
        <label>
          <span>{t("workspaceGraph.drawer.folderRule")}</span>
          <select disabled={!selectedValue} onChange={(event) => setMode(event.currentTarget.value as WorkspaceGraphFacetMode)} value={mode}>
            {(["include", "exclude", "only"] as const).map((value) =>
              <option key={value} value={value}>{modeLabel(value)}</option>)}
          </select>
        </label>
        <button className="workspace-graph-folder-rule-commit" disabled={!selectedValue || selectedRule?.mode === mode} onClick={() => {
          onChange(updateFacetRule(filters, { facet: "folder", mode, value: selectedValue }));
          setNotice(t("workspaceGraph.drawer.ruleStaged", { label: labelFor(selectedValue), mode: modeLabel(mode) }));
        }} type="button">
          {t(selectedRule ? "workspaceGraph.drawer.updateFolderRule" : "workspaceGraph.drawer.addFolderRule")}
        </button>
      </div>
      <small>{t("workspaceGraph.drawer.onlyNote")}</small>
      <p aria-live="polite" className="workspace-graph-folder-notice" role="status">{notice}</p>
      <details className="workspace-graph-folder-rules">
        <summary>{t("workspaceGraph.drawer.folderRules", { count: rules.length })}</summary>
        {rules.length ? <ul>
          {rules.map((rule) => <li key={rule.value}>
            <button aria-label={t("workspaceGraph.drawer.editFolderRule", { label: labelFor(rule.value) })}
              className="workspace-graph-folder-rule-chip" onClick={() => {
                setSearch(""); setSelected(rule.value); setMode(rule.mode); setNotice("");
                searchRef.current?.focus();
              }} type="button">
              <span>{modeLabel(rule.mode)} · {labelFor(rule.value)}</span>
              <small>{available.has(rule.value)
                ? t("workspaceGraph.drawer.visibleCandidates", { count: available.get(rule.value)!.count })
                : t("workspaceGraph.drawer.facetUnavailable")}</small>
            </button>
            <button aria-label={t("workspaceGraph.drawer.removeFolderRule", { label: labelFor(rule.value) })}
              className="workspace-graph-folder-rule-remove" onClick={() => {
                onChange(removeFacetRule(filters, "folder", rule.value));
                setNotice(t("workspaceGraph.drawer.ruleRemoved", { label: labelFor(rule.value) }));
                searchRef.current?.focus();
              }} type="button"><X aria-hidden="true" size={16} /></button>
          </li>)}
        </ul> : <p>{t("workspaceGraph.drawer.noFolderRules")}</p>}
      </details>
    </div>
  );
}
