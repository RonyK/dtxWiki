import { ArrowRight, Lock, Search, Settings2 } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
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
on. Both are listed in a folded "always on or external" list; only the pill
beside them differs. */
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

/** Read-only viewers get the state as a word; a greyed checkbox hides it.

`null` means this browser has not confirmed the value yet. The server always
applies a concrete value, so an unconfirmed one must never read as Off. */
function StateWord({ on }: { on: boolean | null }) {
  const { t } = useTranslation("settings");
  const state = on === null ? "unknown" : on ? "on" : "off";
  return (
    <span className={`lint-state lint-state--${state}`}>
      {t(on === null ? "lint.unknownValue" : on ? "lint.stateOn" : "lint.stateOff")}
    </span>
  );
}

function ChangedPill({ show }: { show: boolean }) {
  const { t } = useTranslation("settings");
  return show ? <StatusPill status="edited" label={t("lint.changed")} tone="warn" compact /> : null;
}

/** One option's effect, labelled "Now" or, while that option is edited, "After saving". */
function OptionEffect({ id, next, text }: { id: string; next: boolean; text: string }) {
  const { t } = useTranslation("settings");
  return (
    <p className={`lint-effect${next ? " lint-effect--next" : ""}`} id={id}>
      <span className="lint-effect-label">{t(next ? "lint.effectNext" : "lint.effectNow")}</span>
      {/* Keeps "Now" and the sentence apart when read as one description. */}
      {" "}
      <span>{text}</span>
    </p>
  );
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

  // Counts losses of edit permission. A save that was pending across one must
  // not hand back the edits that loss discarded. Updated during commit, so a
  // response that arrives right after the change already sees it.
  const permissionLosses = useRef(0);
  useLayoutEffect(() => {
    if (readOnly) permissionLosses.current += 1;
  }, [readOnly]);

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
  const dirty = dirtyCount > 0 && !readOnly;
  const isChanged = (key: string) => !readOnly && key in changes;
  useRegisterSettingsDirty("policy:lint", dirty);
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
    const losses = permissionLosses.current;
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
      const isConflict = caught instanceof ApiError && caught.body.error_code === "settings_revision_conflict";
      if (permissionLosses.current !== losses) {
        // Edit permission was lost while this save was pending and its edits
        // were discarded then. Neither restore nor report them; a conflict
        // still means the shown baseline is stale.
        if (isConflict) {
          setCard(null);
          setPolicyAttempt((value) => value + 1);
        }
      } else if (isConflict) {
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

  const searching = query.trim().length > 0;
  const optionalRules = (rules ?? []).filter(isEditable);
  const optionalOn = optionalRules.filter((rule) => rule.enabled).length;
  const defaultOffCount = optionalRules.filter((rule) => !rule.default_enabled).length;
  const lockedCount = (rules?.length ?? 0) - optionalRules.length;
  const changedChecks = (rules ?? []).filter((rule) => isChanged(rule.rule_id)).length;
  const linkModeLabel = linkFixMode === "auto"
    ? t("lint.linkFixAuto")
    : linkFixMode === "suggest" ? t("lint.linkFixSuggest") : t("lint.unknownValue");

  // A control announces what it does and, once its value is confirmed, its
  // current (or after-saving) effect.
  function describedBy(id: string) {
    return policyReady ? `${id}-hint ${id}-effect` : `${id}-hint`;
  }

  function optionControl(id: string, value: unknown, onChange: () => void) {
    return readOnly ? <StateWord on={typeof value === "boolean" ? value : null} /> : (
      <input
        id={id}
        type="checkbox"
        checked={value === true}
        aria-describedby={describedBy(id)}
        title={value === null ? t("lint.unknownValue") : undefined}
        ref={(input) => { if (input) input.indeterminate = value === null; }}
        disabled={disabled}
        onChange={onChange}
      />
    );
  }

  function optionLabel(id: string, text: string) {
    return readOnly ? <span>{text}</span> : <label htmlFor={id}>{text}</label>;
  }

  return (
    <section className="feature-section">
      <div className={`policy-card lint-rules-card${dirty ? " lint-rules-card--dirty" : ""}${readOnly ? " lint-rules-card--readonly" : ""}`}>
        <div className="lint-rules-header">
          <h2 className="metric-card-title policy-card-heading">
            <Settings2 size={16} aria-hidden="true" />
            <span>{t("lint.eyebrow", { defaultValue: "Lint rules" })}</span>
            <PolicyStateChip
              label={
                dirty ? t("state.unsaved", { count: dirtyCount }) : readOnly
                  ? t("state.ownerOnly", { defaultValue: "Owner only" })
                  : t("state.editable", { defaultValue: "Editable" })
              }
              state={readOnly ? "ownerOnly" : dirty ? "unsaved" : "editable"}
            />
          </h2>
        </div>
        <p className="lint-rules-description">{t("lint.description")}</p>
        {readOnly ? <StatusBanner tone="info">{t("lint.readOnlyNotice")}</StatusBanner> : null}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        {conflict && policyReady ? <StatusBanner tone="warning">{t("lint.revisionConflict")}</StatusBanner> : null}
        {policyError ? (
          <StatusBanner tone="warning">
            <span>{t("lint.policyLoadError")}</span>{" "}
            <button aria-label={t("lint.retryPolicies")} className="text-button" type="button" onClick={() => setPolicyAttempt((value) => value + 1)}>
              {t("shell:actions.retry")}
            </button>
          </StatusBanner>
        ) : !policyReady ? <SettingsLoadingRow>{t("lint.policyLoading")}</SettingsLoadingRow> : null}
        {rulesError ? (
          <StatusBanner tone="warning">
            <span>{t("lint.loadError")}</span>{" "}
            <button aria-label={t("lint.retryRules")} className="text-button" type="button" onClick={() => setRulesAttempt((value) => value + 1)}>
              {t("shell:actions.retry")}
            </button>
          </StatusBanner>
        ) : null}

        <section className="lint-settings-block" aria-labelledby="lint-options-title">
          <h3 id="lint-options-title" className="lint-settings-block-title">{t("lint.optionsTitle")}</h3>
          <div className="lint-settings-group" role="group" aria-labelledby="lint-save-heading">
            <h4 id="lint-save-heading">{t("lint.saveGroup")}</h4>
            <div className={`lint-option${isChanged("auto_fix") ? " lint-option--changed" : ""}`}>
              {optionControl("lint-auto-fix", autoFix, () => updateDraft("auto_fix", !autoFix))}
              <div className="lint-rule-copy">
                <span className="lint-rule-label">
                  {optionLabel("lint-auto-fix", t("lint.autoFix", { defaultValue: "Auto-fix safe formatting on save" }))}
                  <ChangedPill show={isChanged("auto_fix")} />
                </span>
                <span className="lint-rule-description" id="lint-auto-fix-hint">{t("lint.autoFixHint")}</span>
                {policyReady ? (
                  <OptionEffect
                    id="lint-auto-fix-effect"
                    next={isChanged("auto_fix")}
                    text={t(autoFix ? "lint.effectFormatOn" : "lint.effectFormatOff")}
                  />
                ) : null}
              </div>
            </div>
            <div className={`lint-option lint-option--select${isChanged("link_fix_mode") ? " lint-option--changed" : ""}`}>
              <div className="lint-rule-copy">
                <span className="lint-rule-label">
                  {optionLabel("lint-link-fix-mode", t("lint.linkFixMode"))}
                  <ChangedPill show={isChanged("link_fix_mode")} />
                </span>
                <span className="lint-rule-description" id="lint-link-fix-mode-hint">{t("lint.linkFixModeHint")}</span>
                {policyReady ? (
                  <OptionEffect
                    id="lint-link-fix-mode-effect"
                    next={isChanged("link_fix_mode")}
                    text={t(linkFixMode === "auto" ? "lint.effectLinksAuto" : "lint.effectLinksSuggest")}
                  />
                ) : null}
                <span className="lint-option-note">{t("lint.indexRefHint")}</span>
              </div>
              {readOnly ? <span className="lint-option-value">{linkModeLabel}</span> : (
                <select
                  id="lint-link-fix-mode"
                  aria-describedby={describedBy("lint-link-fix-mode")}
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
              )}
            </div>
          </div>
          <div className="lint-settings-group" role="group" aria-labelledby="lint-review-heading">
            <h4 id="lint-review-heading">{t("lint.reviewGroup")}</h4>
            <div className={`lint-option${isChanged("auto_approve_link_fixes") ? " lint-option--changed" : ""}`}>
              {optionControl("lint-auto-approve-link-fixes", autoApproveLinkFixes,
                () => updateDraft("auto_approve_link_fixes", !autoApproveLinkFixes))}
              <div className="lint-rule-copy">
                <span className="lint-rule-label">
                  {optionLabel("lint-auto-approve-link-fixes", t("lint.autoApproveLinkFixes"))}
                  <ChangedPill show={isChanged("auto_approve_link_fixes")} />
                </span>
                <span className="lint-rule-description" id="lint-auto-approve-link-fixes-hint">
                  {t("lint.autoApproveLinkFixesHint")}
                </span>
                {policyReady ? (
                  <OptionEffect
                    id="lint-auto-approve-link-fixes-effect"
                    next={isChanged("auto_approve_link_fixes")}
                    text={t(autoApproveLinkFixes ? "lint.effectApprovalOn" : "lint.effectApprovalOff")}
                  />
                ) : null}
              </div>
            </div>
          </div>
        </section>

        <section className="lint-settings-block lint-checks-block" aria-labelledby="lint-checks-title">
          <div className="lint-settings-block-head">
            <h3 id="lint-checks-title" className="lint-settings-block-title">{t("lint.checks")}</h3>
            <Link className="lint-queue-link" to={workspacePath(workspaceId, "lint")}
              onClick={(event) => {
                // A modified or non-primary click opens elsewhere and loses nothing.
                if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                // Let the existing Settings guard also cover the queue link.
                let allowed = false;
                guardNavigation(() => { allowed = true; });
                if (!allowed) event.preventDefault();
              }}
            >
              {t("lint.queueLink")}
              <ArrowRight size={14} aria-hidden="true" />
            </Link>
          </div>
          {rules && rules.length > 0 ? (
            <p className="lint-checks-summary">
              <strong>{t("lint.checksSummary", { enabled: optionalOn, total: optionalRules.length })}</strong>
              {defaultOffCount ? <span> · {t("lint.defaultOffCount", { count: defaultOffCount })}</span> : null}
              {lockedCount ? <span> · {t("lint.lockedCount", { count: lockedCount })}</span> : null}
              {changedChecks ? (
                <span className="lint-checks-changed"> · {t("lint.changedCount", { count: changedChecks })}</span>
              ) : null}
            </p>
          ) : null}
          <details className="lint-checks">
            <summary>{rules ? t("lint.showChecks", { count: rules.length }) : t("lint.showChecksPending")}</summary>
            <p>{t("lint.checksHint")}</p>
            <label className="lint-rule-search">
              <Search size={16} aria-hidden="true" />
              <span className="visually-hidden">{t("lint.searchRules")}</span>
              <input
                type="search"
                value={query}
                placeholder={t("lint.searchPlaceholder")}
                onChange={(event) => setQuery(event.target.value)}
              />
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
              const toggleable = layerRules.filter(isEditable);
              const locked = layerRules.filter((rule) => !isEditable(rule));
              const enabledCount = toggleable.filter((rule) => rule.enabled).length;
              const allOn = toggleable.length > 0 && enabledCount === toggleable.length;
              const layerName = t(`lint.layer.${layer}`, { defaultValue: layer });
              // A bulk action reaches only the rows the search shows, and says so.
              const action = t(searching
                ? (allOn ? "lint.layerShownOff" : "lint.layerShownOn")
                : (allOn ? "lint.layerAllOff" : "lint.layerAllOn"));
              return (
                <div key={layer} className="lint-rule-group">
                  <div className="lint-rule-group-header">
                    <h4>
                      {layerName}
                      <span className="lint-rule-group-count">
                        {toggleable.length
                          ? t(searching ? "lint.groupCountShown" : "lint.groupCount", {
                            enabled: enabledCount,
                            total: toggleable.length,
                          })
                          : t("lint.groupReadOnly")}
                      </span>
                    </h4>
                    {!readOnly && toggleable.length > 0 ? (
                      <button
                        type="button"
                        className="text-button lint-rule-group-action"
                        aria-label={t("lint.layerActionAria", { action, layer: layerName })}
                        disabled={disabled}
                        onClick={() => toggleLayer(toggleable, !allOn)}
                      >
                        {action}
                      </button>
                    ) : null}
                  </div>
                  {toggleable.some((rule) => !rule.default_enabled) ? (
                    <p className="lint-rule-group-note">{t("lint.defaultOffNote")}</p>
                  ) : null}
                  {toggleable.length ? (
                    <ul className="lint-rule-list">
                      {toggleable.map((rule) => {
                        const inputId = `lint-rule-${rule.rule_id.replaceAll(".", "-")}`;
                        const label = lintRuleLabel(lintT, rule.rule_id);
                        const changed = isChanged(rule.rule_id);
                        return (
                          // The rule id is diagnostic detail, not the rule's name:
                          // it stays reachable on hover instead of on screen.
                          <li
                            className={`lint-rule-row${changed ? " lint-rule-row--changed" : ""}${busy ? " lint-rule-row--busy" : ""}`}
                            key={rule.rule_id}
                          >
                            {readOnly ? <StateWord on={rule.enabled} /> : (
                              <input
                                id={inputId}
                                type="checkbox"
                                checked={rule.enabled}
                                disabled={disabled}
                                onChange={() => toggle(rule)}
                              />
                            )}
                            <div className="lint-rule-copy">
                              <span className="lint-rule-label">
                                {readOnly
                                  ? <span title={rule.rule_id}>{label}</span>
                                  : <label htmlFor={inputId} title={rule.rule_id}>{label}</label>}
                                {rule.default_enabled ? null : (
                                  <StatusPill status="default_off" label={t("lint.defaultOff")} tone="muted" compact />
                                )}
                                <ChangedPill show={changed} />
                              </span>
                              <span className="lint-rule-description">{lintRuleDescription(lintT, rule.rule_id)}</span>
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  ) : null}
                  {locked.length ? (
                    // Rows nobody can toggle stay one click away; a search that
                    // matches them opens the fold.
                    <details className="lint-locked-rules" open={searching || undefined}>
                      <summary>{t("lint.lockedCount", { count: locked.length })}</summary>
                      <ul className="lint-rule-list">
                        {locked.map((rule) => (
                          <li className="lint-rule-row" key={rule.rule_id}>
                            <Lock aria-hidden="true" className="lint-rule-lock" size={14} />
                            <div className="lint-rule-copy">
                              <span className="lint-rule-label">
                                <span title={rule.rule_id}>{lintRuleLabel(lintT, rule.rule_id)}</span>
                                <StatusPill
                                  status={rule.policy_locked ? "policy_locked" : "external_source"}
                                  label={rule.policy_locked ? t("lint.requiredSafeguard") : t("lint.externalSource")}
                                  tone="info"
                                  compact
                                />
                              </span>
                              <span className="lint-rule-description">{lintRuleDescription(lintT, rule.rule_id)}</span>
                            </div>
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                </div>
              );
            })}
          </details>
        </section>

        {!readOnly ? (
          // While edits are pending this bar sticks to the bottom of the
          // screen, so Save stays in reach anywhere in the long card.
          <div className="policy-card-actions">
            <span aria-live="polite" className={dirty ? "lint-dirty-summary" : "policy-card-saved"}>
              {dirty ? t("lint.dirtySummary", { count: dirtyCount }) : savedNotice}
            </span>
            <button className="icon-button" type="button" disabled={!dirtyCount || busy} onClick={discardDrafts}>
              {t("actions.discard")}
            </button>
            <button className="icon-button primary" type="button" disabled={disabled || !dirtyCount}
              title={t(busy ? "saveTitle.busy" : dirtyCount ? "saveTitle.ready" : "saveTitle.clean")}
              onClick={() => void saveDrafts()}>
              {busy ? t("actions.saving") : t("shell:actions.save")}
            </button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
