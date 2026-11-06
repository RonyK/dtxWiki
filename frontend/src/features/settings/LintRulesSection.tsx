import { Lock, Settings2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  getLintRules,
  getSettingsPolicies,
  updateSettingsPolicyBatch,
  type PolicyCard,
  type LintRuleInfo,
} from "../../shared/api/operations";
import { ApiError } from "../../shared/api/client";
import { useRegisterSettingsDirty, useGuardedSettingsChange } from "./SettingsDirtyRegistry";
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

// The policy card is the saved baseline. Every control edits a local draft;
// only Save changes writes the workspace policy, as one revision-guarded batch.
export function LintRulesSection({ readOnly, workspaceId: routeWorkspaceId }: { readOnly: boolean; workspaceId: string }) {
  // A workspace switch discards private edits and reloads the saved view.
  return <WorkspaceLintRules key={routeWorkspaceId} readOnly={readOnly} workspaceId={routeWorkspaceId} />;
}

/** The lint card when it is a complete baseline for editing, otherwise null. */
function validCard(card: PolicyCard | undefined): PolicyCard | null {
  const policy = card?.policy;
  return card && policy && /^[a-f0-9]{64}$/i.test(card.revision) &&
    typeof policy.auto_fix === "boolean" &&
    (policy.link_fix_mode === "auto" || policy.link_fix_mode === "suggest") &&
    typeof policy.auto_approve_link_fixes === "boolean" ? card : null;
}

function WorkspaceLintRules({ readOnly, workspaceId }: { readOnly: boolean; workspaceId: string }) {
  const { t } = useTranslation("settings");
  const { t: lintT } = useTranslation("lint");
  const [catalog, setCatalog] = useState<LintRuleInfo[] | null>(null);
  const [card, setCard] = useState<PolicyCard | null>(null);
  const [drafts, setDrafts] = useState<Record<string, unknown>>({});
  const [policyError, setPolicyError] = useState(false);
  const [rulesError, setRulesError] = useState(false);
  const [policyAttempt, setPolicyAttempt] = useState(0);
  const [rulesAttempt, setRulesAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const active = useRef(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A save hit a newer revision; announced once that revision has loaded.
  const [conflict, setConflict] = useState(false);
  const [savedNotice, setSavedNotice] = useState("");
  const [wasReadOnly, setWasReadOnly] = useState(readOnly);
  const guardNavigation = useGuardedSettingsChange(t("nav.discardDraftsConfirm"));

  function clearNotices() {
    setError(null);
    setConflict(false);
    setSavedNotice("");
  }

  // Losing edit permission discards private edits; the saved view stays.
  if (wasReadOnly !== readOnly) {
    setWasReadOnly(readOnly);
    if (readOnly) {
      setDrafts({});
      clearNotices();
    }
  }

  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setRulesError(false);
    void getLintRules(workspaceId).then((data) => {
      if (!cancelled) setCatalog(data.rules);
    }).catch(() => { if (!cancelled) setRulesError(true); });
    return () => { cancelled = true; };
  }, [workspaceId, rulesAttempt]);

  useEffect(() => {
    let cancelled = false;
    setPolicyError(false);
    void getSettingsPolicies(workspaceId).then((policies) => {
      const loaded = validCard(policies.lint);
      if (cancelled) return;
      if (loaded) setCard(loaded);
      else setPolicyError(true);
    }).catch(() => { if (!cancelled) setPolicyError(true); });
    return () => { cancelled = true; };
  }, [workspaceId, policyAttempt]);

  const policyReady = card !== null && !policyError;
  const ready = policyReady && catalog !== null && !rulesError;
  const disabled = readOnly || !ready || busy;
  // Overrides come from the same revision as the options; the catalog adds
  // defaults and capabilities. Until that revision loads, the catalog's own
  // server-computed state stands in, so the list never shows bare defaults.
  function savedRuleValue(rule: LintRuleInfo) {
    return card ? card.policy[rule.rule_id] ?? rule.default_enabled : rule.enabled;
  }
  function savedValue(key: string) {
    const rule = catalog?.find((item) => item.rule_id === key);
    return rule ? savedRuleValue(rule) : card?.policy[key];
  }
  const changes = Object.fromEntries(Object.entries(drafts).filter(([key, value]) => value !== savedValue(key)));
  const dirtyCount = Object.keys(changes).length;
  useRegisterSettingsDirty("policy:lint", dirtyCount > 0 && !readOnly);
  const policy = { ...card?.policy, ...drafts };
  const autoFix = policyReady ? policy.auto_fix : null;
  const linkFixMode = policyReady ? policy.link_fix_mode : null;
  const autoApproveLinkFixes = policyReady ? policy.auto_approve_link_fixes : null;
  const rules = catalog?.map((rule) => ({
    ...rule,
    enabled: rule.policy_locked ||
      (rule.rule_id in drafts ? drafts[rule.rule_id] : savedRuleValue(rule)) === true,
  })) ?? null;

  function updateDraft(key: string, value: unknown) {
    if (disabled) return;
    clearNotices();
    setDrafts((current) => ({ ...current, [key]: value }));
  }

  function toggle(rule: LintRuleInfo) {
    if (isEditable(rule)) updateDraft(rule.rule_id, !rule.enabled);
  }

  function toggleLayer(layerRules: LintRuleInfo[], enabled: boolean) {
    if (disabled) return;
    clearNotices();
    setDrafts((current) => ({
      ...current,
      ...Object.fromEntries(layerRules.filter(isEditable).map((rule) => [rule.rule_id, enabled])),
    }));
  }

  function discardDrafts() {
    if (busy) return;
    setDrafts({});
    clearNotices();
  }

  async function saveDrafts() {
    if (disabled || !card || !dirtyCount) return;
    setBusy(true);
    clearNotices();
    try {
      const saved = await updateSettingsPolicyBatch(workspaceId, {
        group: "lint",
        expected_revision: card.revision,
        changes,
      });
      if (!active.current) return;
      // The batch has committed. An unusable response card only means the
      // baseline must be reloaded; it is never reported as a failed save.
      const nextCard = validCard(saved.lint);
      setCard(nextCard);
      if (!nextCard) setPolicyAttempt((value) => value + 1);
      setDrafts({});
      setSavedNotice(t("lint.saved"));
    } catch (caught) {
      if (!active.current) return;
      if (caught instanceof ApiError && caught.body.error_code === "settings_revision_conflict") {
        // Keep the attempted edits and reload only the policy baseline: the
        // catalog's defaults and capabilities do not depend on its revision.
        // Unedited values come from the latest card.
        setDrafts(changes);
        setCard(null);
        setConflict(true);
        setPolicyAttempt((value) => value + 1);
      } else {
        setError(t("lint.saveError"));
      }
    } finally {
      if (active.current) setBusy(false);
    }
  }

  function matchesSearch(rule: LintRuleInfo) {
    const needle = query.trim().toLocaleLowerCase();
    return [rule.rule_id, lintRuleLabel(lintT, rule.rule_id), lintRuleDescription(lintT, rule.rule_id)]
      .some((value) => value.toLocaleLowerCase().includes(needle));
  }

  return (
    <section className="feature-section">
      {/* F-7: match the other settings sections — a policy-card with an
          icon+title header + description, not bare checkboxes. */}
      <div className="policy-card lint-rules-card">
        <div className="lint-rules-header">
          {/* Same header contract as PolicyGroupView: title + state chip on
              the left, the card-level action pinned right. */}
          <h2 className="metric-card-title policy-card-heading">
            <Settings2 size={16} aria-hidden="true" />
            <span>{t("lint.eyebrow", { defaultValue: "Lint rules" })}</span>
            <PolicyStateChip
              label={
                dirtyCount > 0 && !readOnly ? t("state.unsaved", { count: dirtyCount }) : readOnly
                  ? t("state.ownerOnly", { defaultValue: "Owner only" })
                  : t("state.editable", { defaultValue: "Editable" })
              }
              state={readOnly ? "ownerOnly" : dirtyCount > 0 ? "unsaved" : "editable"}
            />
          </h2>
          <Link className="icon-button text-button" to={workspacePath(workspaceId, "lint")}
            onClick={(event) => {
              // A modified or non-primary click opens elsewhere and loses nothing.
              if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
              // Let the existing Settings guard also cover the queue link.
              let allowed = false;
              guardNavigation(() => { allowed = true; });
              if (!allowed) event.preventDefault();
            }}
          >
            {t("lint.openQueue", { defaultValue: "Open lint queue" })}
          </Link>
        </div>
        <p>{t("lint.description")}</p>
        {readOnly ? null : <p className="meta-line">{t("lint.workspaceSaveHint")}</p>}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        {conflict && policyReady ? <StatusBanner tone="warning">{t("lint.revisionConflict")}</StatusBanner> : null}
        {policyError ? (
          <StatusBanner tone="warning">
            <span>{t("lint.policyLoadError")}</span>{" "}
            <button className="text-button" type="button" onClick={() => setPolicyAttempt((value) => value + 1)}>
              {t("lint.retryPolicies")}
            </button>
          </StatusBanner>
        ) : !policyReady ? <SettingsLoadingRow>{t("lint.policyLoading")}</SettingsLoadingRow> : null}
        {rulesError ? (
          <StatusBanner tone="warning">
            <span>{t("lint.loadError")}</span>{" "}
            <button className="text-button" type="button" onClick={() => setRulesAttempt((value) => value + 1)}>
              {t("lint.retryRules")}
            </button>
          </StatusBanner>
        ) : null}
        <div className="lint-settings-group" role="group" aria-labelledby="lint-save-heading">
          <h3 id="lint-save-heading">{t("lint.saveGroup")}</h3>
          <div className="lint-autofix lint-rule-row">
            <input
              id="lint-auto-fix"
              type="checkbox"
              checked={autoFix === true}
              title={autoFix === null ? t("lint.unknownValue") : undefined}
              ref={(input) => { if (input) input.indeterminate = autoFix === null; }}
              disabled={disabled}
              onChange={() => updateDraft("auto_fix", !autoFix)}
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
                {t("lint.autoFixHint")}
              </span>
            </div>
          </div>
          <div className="lint-autofix lint-link-fix-policy">
            <label className="lint-rule-copy" htmlFor="lint-link-fix-mode">
              <span className="lint-rule-label">{t("lint.linkFixMode")}</span>
              <span className="lint-rule-description">{t("lint.linkFixModeHint")}</span>
            </label>
            <select
              id="lint-link-fix-mode"
              value={typeof linkFixMode === "string" ? linkFixMode : ""}
              disabled={disabled}
              onChange={(event) => updateDraft("link_fix_mode", event.target.value)}
            >
              {linkFixMode === null ? (
                <option value="" disabled aria-label={t("lint.unknownValue")}>—</option>
              ) : null}
              <option value="auto">{t("lint.linkFixAuto")}</option>
              <option value="suggest">{t("lint.linkFixSuggest")}</option>
            </select>
          </div>
        </div>
        <div className="lint-settings-group" role="group" aria-labelledby="lint-review-heading">
          <h3 id="lint-review-heading">{t("lint.reviewGroup")}</h3>
          <div className="lint-autofix lint-rule-row">
            <input
              id="lint-auto-approve-link-fixes"
              type="checkbox"
              checked={autoApproveLinkFixes === true}
              aria-describedby="lint-auto-approve-link-fixes-hint"
              title={autoApproveLinkFixes === null ? t("lint.unknownValue") : undefined}
              ref={(input) => { if (input) input.indeterminate = autoApproveLinkFixes === null; }}
              disabled={disabled}
              onChange={() => updateDraft("auto_approve_link_fixes", !autoApproveLinkFixes)}
            />
            <div className="lint-rule-copy">
              <span className="lint-rule-label">
                <label htmlFor="lint-auto-approve-link-fixes">{t("lint.autoApproveLinkFixes")}</label>
              </span>
              <span className="lint-rule-description" id="lint-auto-approve-link-fixes-hint">
                {t("lint.autoApproveLinkFixesHint")}
              </span>
            </div>
          </div>
        </div>
        {policyReady ? (
          <div className="lint-current-effect">
            <h3>{t(dirtyCount > 0 ? "lint.effectAfterSaving" : "lint.currentEffect")}</h3>
            <p>{t(linkFixMode === "auto" ? "lint.effectLinksAuto" : "lint.effectLinksSuggest")}</p>
            <p>{t(autoFix ? "lint.effectFormatOn" : "lint.effectFormatOff")}</p>
            <p>{t(autoApproveLinkFixes ? "lint.effectApprovalOn" : "lint.effectApprovalOff")}</p>
          </div>
        ) : null}
        <p className="lint-fixed-guidance">{t("lint.indexRefHint")}</p>
        <details className="lint-checks">
          <summary><h3>{t("lint.checks")}</h3></summary>
          <p>{t("lint.checksHint")}</p>
          <label className="lint-rule-search">
            <span>{t("lint.searchRules")}</span>
            <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>
          {rules === null && !rulesError ? <SettingsLoadingRow>{t("lint.loading")}</SettingsLoadingRow> : null}
          {rules !== null && rules.length === 0 ? (
            <SettingsEmptyState
              description={t("lint.emptyHint")}
              title={t("lint.empty", { defaultValue: "No lint rules" })}
            />
          ) : null}
          {rules && rules.length > 0 && !rules.some(matchesSearch) ? <p>{t("lint.noMatches")}</p> : null}
          {LAYER_ORDER.map((layer) => {
            const layerRules = (rules ?? []).filter((rule) => rule.layer === layer && matchesSearch(rule));
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
                  <h4>{t(`lint.layer.${layer}`, { defaultValue: layer })}</h4>
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
                        disabled={disabled}
                        enabledCount={enabledCount}
                        total={toggleable.length}
                        onToggle={(next) => toggleLayer(toggleable, next)}
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
                          busy ? "lint-rule-row--busy" : ""
                        }`}
                        key={rule.rule_id}
                      >
                        {isEditable(rule) ? (
                          <input
                            id={inputId}
                            type="checkbox"
                            checked={rule.enabled}
                            disabled={disabled}
                            onChange={() => toggle(rule)}
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
        </details>
        {!readOnly ? (
          <div className="policy-card-actions">
            <span aria-live="polite" className="policy-card-saved">{savedNotice}</span>
            <button className="icon-button" type="button" disabled={!dirtyCount || busy} onClick={discardDrafts}>
              {t("actions.discard")}
            </button>
            <button className="icon-button primary" type="button" disabled={disabled || !dirtyCount}
              title={t(busy ? "saveTitle.busy" : dirtyCount ? "saveTitle.ready" : "saveTitle.clean")}
              onClick={() => void saveDrafts()}>
              {busy ? t("actions.saving") : t("actions.save")}
            </button>
          </div>
        ) : null}
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
