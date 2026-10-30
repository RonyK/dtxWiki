import { Lock, Settings2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  getLintRules,
  getSettingsPolicies,
  updateSettingsPolicy,
  type LintRuleInfo,
} from "../../shared/api/operations";
import { workspacePath } from "../../shared/lib/workspaceRoutes";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import {
  PolicyStateChip,
  SettingsEmptyState,
  SettingsLoadingRow,
} from "./SettingsPaneParts";
import { lintRuleDescription, lintRuleLabel } from "../lint/lintRuleCatalog";

const LAYER_ORDER = ["format", "knowledge", "nav", "maintenance"] as const;

/** Whether this row's checkbox does anything.

A rule is un-toggleable for two different reasons: it needs an external source
the workspace does not run, or it is a safeguard the publication floor depends
on. Both render as locked; only the pill beside them differs. */
function isEditable(rule: LintRuleInfo): boolean {
  return rule.runtime_checkable && !rule.policy_locked;
}

// Settings > Lint: per-workspace rule toggles, grouped by layer. The catalog
// (GET /lint/rules) carries every rule's effective enabled state; flipping a
// toggle writes the `lint` policy ({rule_id: bool}) via updateSettingsPolicy.
export function LintRulesSection({ readOnly, workspaceId: routeWorkspaceId }: { readOnly: boolean; workspaceId: string }) {
  const { t } = useTranslation("settings");
  const { t: lintT } = useTranslation("lint");
  const [rules, setRules] = useState<LintRuleInfo[] | null>(null);
  const [autoFix, setAutoFix] = useState(false);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  // Per-rule, so saving one rule no longer freezes every other checkbox on the
  // page. `auto_fix` shares the set under its own key.
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [savedNotice, setSavedNotice] = useState("");

  useEffect(() => {
    // Load once on mount. Depending on `t` (whose identity changes when the
    // user switches language) would refetch and clobber state on every language
    // toggle; labels read `t` at render time instead. (M10/I1)
    let cancelled = false;
    setWorkspaceId(routeWorkspaceId);
    setRules(null);
    setAutoFix(false);
    setError(null);
    (async () => {
      try {
        const wid = routeWorkspaceId;
        const data = await getLintRules(wid);
        if (!cancelled) {
          setWorkspaceId(wid);
          setRules(data.rules);
          setError(null);
        }
        // I10: auto-fix is a secondary concern — a policies-fetch failure must
        // NOT hide the rules list. Load it independently and degrade to "off".
        try {
          const policies = await getSettingsPolicies(wid);
          if (!cancelled) {
            setAutoFix(Boolean(policies.lint?.policy?.auto_fix));
          }
        } catch {
          if (!cancelled) {
            setAutoFix(false);
          }
        }
      } catch {
        if (!cancelled) {
          setError(t("lint.loadError", { defaultValue: "Failed to load lint rules." }));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeWorkspaceId]);

  function markBusy(keys: string[], value: boolean) {
    setBusy((current) => {
      const next = new Set(current);
      for (const key of keys) {
        if (value) {
          next.add(key);
        } else {
          next.delete(key);
        }
      }
      return next;
    });
  }

  function setRuleEnabled(ruleId: string, enabled: boolean) {
    setRules((current) =>
      current
        ? current.map((item) =>
            item.rule_id === ruleId ? { ...item, enabled } : item,
          )
        : current,
    );
  }

  function announceSaved() {
    setSavedNotice(t("lint.saved", { defaultValue: "Saved" }));
  }

  // Optimistic: the checkbox moves on click and rolls back if the write fails,
  // so the control itself is the save feedback. Callers own the busy marking —
  // a rule must stay locked for as long as any write covering it is in flight,
  // which a per-write flag cannot express once one write spans several rules.
  async function writeRule(ruleId: string, enabled: boolean, previous: boolean) {
    if (!workspaceId) {
      return false;
    }
    setRuleEnabled(ruleId, enabled);
    try {
      await updateSettingsPolicy(workspaceId, {
        group: "lint",
        key: ruleId,
        value: enabled,
      });
      return true;
    } catch {
      setRuleEnabled(ruleId, previous);
      return false;
    }
  }

  async function toggle(rule: LintRuleInfo) {
    if (busy.has(rule.rule_id)) {
      return;
    }
    setError(null);
    setSavedNotice("");
    markBusy([rule.rule_id], true);
    try {
      if (await writeRule(rule.rule_id, !rule.enabled, rule.enabled)) {
        announceSaved();
      } else {
        setError(t("lint.saveError", { defaultValue: "Failed to update the rule." }));
      }
    } finally {
      markBusy([rule.rule_id], false);
    }
  }

  async function toggleLayer(layerRules: LintRuleInfo[], enabled: boolean) {
    const targets = layerRules.filter(
      (rule) => isEditable(rule) && rule.enabled !== enabled,
    );
    if (targets.length === 0 || targets.some((rule) => busy.has(rule.rule_id))) {
      return;
    }
    setError(null);
    setSavedNotice("");
    // Every rule this toggle will write is locked up front. Writing them one at
    // a time while their rows stayed live let a rule be changed individually
    // mid-run, and the loop would then overwrite that newer choice with the
    // value it captured when the group toggle was clicked.
    const ids = targets.map((rule) => rule.rule_id);
    markBusy(ids, true);
    try {
      let failed = 0;
      for (const rule of targets) {
        if (!(await writeRule(rule.rule_id, enabled, rule.enabled))) {
          failed += 1;
        }
      }
      if (failed > 0) {
        setError(t("lint.saveError", { defaultValue: "Failed to update the rule." }));
      } else {
        announceSaved();
      }
    } finally {
      markBusy(ids, false);
    }
  }

  async function toggleAutoFix() {
    if (!workspaceId) {
      return;
    }
    markBusy(["auto_fix"], true);
    setError(null);
    setSavedNotice("");
    try {
      await updateSettingsPolicy(workspaceId, {
        group: "lint",
        key: "auto_fix",
        value: !autoFix,
      });
      setAutoFix((value) => !value);
      announceSaved();
    } catch {
      setError(t("lint.saveError", { defaultValue: "Failed to update the rule." }));
    } finally {
      markBusy(["auto_fix"], false);
    }
  }

  return (
    <section className="feature-section">
      {/* F-7: match the other settings sections — a policy-card with an
          icon+title header + description, not bare checkboxes. */}
      <div className="policy-card lint-rules-card">
        <div className="lint-rules-header">
          {/* Same header contract as PolicyGroupView: title + state chip on
              the left, the card-level action pinned right. */}
          {/* An h2, not a div: the layer groups below are h3, so with a
              non-heading here the pane jumped h1 -> h3. Measured on the
              running app before this: a worst heading skip of 2, the only
              destination still doing it after Deployment was fixed. */}
          <h2 className="metric-card-title policy-card-heading">
            <Settings2 size={16} aria-hidden="true" />
            <span>{t("lint.eyebrow", { defaultValue: "Lint rules" })}</span>
            <PolicyStateChip
              label={
                readOnly
                  ? t("state.ownerOnly", { defaultValue: "Owner only" })
                  : t("state.editable", { defaultValue: "Editable" })
              }
              state={readOnly ? "ownerOnly" : "editable"}
            />
          </h2>
          <span aria-live="polite" className="policy-card-saved">
            {savedNotice}
          </span>
          <Link className="icon-button text-button" to={workspacePath(routeWorkspaceId, "lint")}>
            {t("lint.openQueue", { defaultValue: "Open lint queue" })}
          </Link>
        </div>
        <p>
          {t("lint.description", {
            defaultValue:
              "Choose deterministic checks for this workspace. Externally "
              + "supplied maintenance findings are read-only and marked as "
              + "External source.",
          })}
        </p>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        {rules === null && !error ? (
          <SettingsLoadingRow>
            {t("lint.loading", { defaultValue: "Loading lint rules…" })}
          </SettingsLoadingRow>
        ) : null}
        <div className="lint-autofix lint-rule-row">
          <input
            id="lint-auto-fix"
            type="checkbox"
            checked={autoFix}
            disabled={readOnly || busy.has("auto_fix")}
            onChange={toggleAutoFix}
          />
          <div className="lint-rule-copy">
            <span className="lint-rule-label">
              <label htmlFor="lint-auto-fix">
                {t("lint.autoFix", {
                  defaultValue: "Auto-fix safe formatting on save",
                })}
              </label>
            </span>
            <span className="lint-rule-description">
              {t("lint.autoFixHint", {
                defaultValue:
                  "When a page is saved, deterministic formatting cleanups are "
                  + "applied — heading and list-marker spacing, trailing "
                  + "whitespace, extra blank lines, and table alignment. Code "
                  + "blocks, wording, and content are never changed.",
              })}
            </span>
          </div>
        </div>
        {/* Lint had no empty state at all: a server that reports no rules
            rendered the auto-fix toggle above and then simply stopped, with
            nothing saying whether the list was empty or still arriving. */}
        {rules !== null && rules.length === 0 ? (
          <SettingsEmptyState
            description={t("lint.emptyHint", {
              defaultValue:
                "This server reports no lint rules, so nothing runs when a page is saved.",
            })}
            title={t("lint.empty", { defaultValue: "No lint rules" })}
          />
        ) : null}
        {LAYER_ORDER.map((layer) => {
          const layerRules = (rules ?? []).filter((rule) => rule.layer === layer);
          if (!layerRules.length) {
            return null;
          }
          // Toggleable rules first: a checkbox column that starts with
          // read-only rows reads as a list of broken checkboxes.
          const ordered = [
            ...layerRules.filter(isEditable),
            ...layerRules.filter((rule) => !isEditable(rule)),
          ];
          const toggleable = layerRules.filter(isEditable);
          const enabledCount = toggleable.filter((rule) => rule.enabled).length;
          return (
            <div key={layer} className="lint-rule-group">
              <div className="lint-rule-group-header">
                <h3>{t(`lint.layer.${layer}`, { defaultValue: layer })}</h3>
                {toggleable.length > 0 ? (
                  <div className="lint-rule-group-toggle">
                    <span className="lint-rule-group-count">
                      {/* Says what the denominator is: it counts the rules this
                          toggle can reach, which in a mixed group is fewer than
                          the rows below it. */}
                      {t("lint.enabledOfTotal", {
                        defaultValue: "{{enabled}} of {{total}} toggleable on",
                        enabled: enabledCount,
                        total: toggleable.length,
                      })}
                    </span>
                    <LayerToggle
                      ariaLabel={t("lint.toggleLayerAria", {
                        defaultValue: "Toggle every {{layer}} rule",
                        layer: t(`lint.layer.${layer}`, { defaultValue: layer }),
                      })}
                      disabled={readOnly || toggleable.some((rule) => busy.has(rule.rule_id))}
                      enabledCount={enabledCount}
                      total={toggleable.length}
                      onToggle={(next) => void toggleLayer(toggleable, next)}
                    />
                  </div>
                ) : null}
              </div>
              <ul className="lint-rule-list">
                {ordered.map((rule) => {
                  const inputId = `lint-rule-${rule.rule_id.replaceAll(".", "-")}`;
                  const label = lintRuleLabel(lintT, rule.rule_id);
                  const description = lintRuleDescription(lintT, rule.rule_id);
                  return (
                    // The rule id is diagnostic detail, not the rule's name:
                    // it stays reachable on hover instead of on screen.
                    <li
                      className={`lint-rule-row ${
                        busy.has(rule.rule_id) ? "lint-rule-row--busy" : ""
                      }`}
                      key={rule.rule_id}
                    >
                      {isEditable(rule) ? (
                        <input
                          id={inputId}
                          type="checkbox"
                          checked={rule.enabled}
                          disabled={readOnly || busy.has(rule.rule_id)}
                          onChange={() => void toggle(rule)}
                        />
                      ) : (
                        <Lock
                          aria-hidden="true"
                          className="lint-rule-lock"
                          size={14}
                        />
                      )}
                      <div className="lint-rule-copy">
                        <span className="lint-rule-label">
                          {/* The rule id rides the label, which is focusable
                              through its checkbox, rather than the row. */}
                          {isEditable(rule) ? (
                            <label htmlFor={inputId} title={rule.rule_id}>
                              {label}
                            </label>
                          ) : (
                            <span title={rule.rule_id}>{label}</span>
                          )}
                          {rule.policy_locked ? (
                            <StatusPill
                              status="policy_locked"
                              label={t("lint.requiredSafeguard", {
                                defaultValue: "Required safeguard",
                              })}
                              tone="info"
                              compact
                            />
                          ) : null}
                          {rule.runtime_checkable || rule.policy_locked ? null : (
                            <StatusPill
                              status="external_source"
                              label={t("lint.externalSource")}
                              tone="info"
                              compact
                            />
                          )}
                        </span>
                        {description ? (
                          <span className="lint-rule-description">{description}</span>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/**
 * Group header checkbox: on when every toggleable rule in the layer is on,
 * indeterminate while the layer is mixed. Clicking a mixed group turns the
 * whole layer on (the same direction a partially-checked group implies).
 */
function LayerToggle({
  ariaLabel,
  disabled,
  enabledCount,
  total,
  onToggle,
}: {
  ariaLabel: string;
  disabled: boolean;
  enabledCount: number;
  total: number;
  onToggle: (enabled: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const allOn = enabledCount === total;
  const mixed = enabledCount > 0 && !allOn;
  useEffect(() => {
    if (ref.current) {
      // `indeterminate` has no HTML attribute — it is DOM-only state.
      ref.current.indeterminate = mixed;
    }
  }, [mixed]);
  // The wrapping label owns the 24px hit area; the accessible name stays on
  // the input so assistive tech reads it exactly as before.
  return (
    <label className="lint-layer-toggle">
      <input
        aria-label={ariaLabel}
        checked={allOn}
        disabled={disabled}
        onChange={() => onToggle(!allOn)}
        ref={ref}
        type="checkbox"
      />
    </label>
  );
}
