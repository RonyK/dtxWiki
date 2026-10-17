import { Check, ChevronDown } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { CountChip } from "../../shared/components/CountChip";
import { Dialog } from "../../shared/components/Dialog";
import { SearchField } from "../../shared/components/SearchField";
import type {
  SettingsDestination,
  SettingsDestinationFlags,
  SettingsPolicyTab,
  SettingsSection,
} from "./SettingsScreen";
import { settingsDestinations, settingsPolicyTabs } from "./SettingsScreen";

/**
 * Section navigation for everything narrower than the desktop two-column
 * layout.
 *
 * It replaces a horizontal strip that could not do the job at any width below
 * 1024px. On a phone the strip measured `scrollWidth 1439` against
 * `clientWidth 340` -- 2.5 of 12 entries -- and its only affordance was a mask
 * that faded BOTH edges unconditionally, so it claimed there was more to the
 * left even at `scrollLeft: 0`. On a tablet the layout collapses to one column
 * and the strip becomes a ~504px vertical list, half the viewport, pushing the
 * first content row to ~963px of 1024.
 *
 * A trigger plus a sheet fixes both at once: one row of chrome, the current
 * destination always legible, and every destination one tap away.
 */
export function SettingsSectionPicker({
  activeSection,
  consentCount,
  onSelect,
  showAttachments,
  showDeployment,
  showUsers,
  tokenCount,
}: {
  activeSection: SettingsSection;
  onSelect: (section: SettingsSection, tab?: SettingsPolicyTab) => void;
} & SettingsDestinationFlags) {
  const { t } = useTranslation("settings");
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  // Hoisted into their own memos (260912 review, P6d) so `matches` below can
  // depend on them honestly. Before this, `sections`/`policyTabs` were
  // rebuilt fresh every render but the `matches` memo depended only on
  // `query` -- so a language switch or a count change (consentCount,
  // tokenCount) while the picker was open left stale labels and badges in
  // the filtered list until the next keystroke re-ran the filter.
  const { sections, groups } = useMemo(
    () =>
      settingsDestinations(t, {
        consentCount,
        showAttachments,
        showDeployment,
        showUsers,
        tokenCount,
      }),
    [t, consentCount, showAttachments, showDeployment, showUsers, tokenCount],
  );
  const current = sections.find((section) => section.id === activeSection);
  const currentGroup = groups.find((group) => group.id === current?.group);

  const policyTabs = useMemo(() => settingsPolicyTabs(t), [t]);
  const matches = useMemo(
    () => filterSettingsDestinations(sections, query, policyTabs),
    [sections, query, policyTabs],
  );

  function choose(section: SettingsSection, tab?: SettingsPolicyTab) {
    setOpen(false);
    setQuery("");
    onSelect(section, tab);
  }

  return (
    <div className="settings-section-picker">
      <button
        aria-expanded={open}
        aria-haspopup="dialog"
        className="settings-section-picker-trigger"
        onClick={() => setOpen(true)}
        ref={triggerRef}
        type="button"
      >
        <span className="settings-section-picker-trigger-text">
          {currentGroup ? (
            <span className="settings-section-picker-trigger-group">
              {currentGroup.label}
            </span>
          ) : null}
          <span className="settings-section-picker-trigger-label">
            {current?.label ??
              t("navigationLabel", { defaultValue: "Settings sections" })}
          </span>
        </span>
        <ChevronDown size={16} aria-hidden="true" />
      </button>

      {open ? (
        <Dialog
          className="settings-section-picker-dialog"
          labelledBy="settings-section-picker-title"
          onClose={() => {
            setOpen(false);
            setQuery("");
          }}
          // The trigger survives the dialog, but resolving it explicitly means
          // focus still lands somewhere sensible if a re-render replaces it.
          restoreFocusFallback={() => triggerRef.current}
        >
          <h2 id="settings-section-picker-title">
            {t("nav.pickerTitle", { defaultValue: "Go to a settings section" })}
          </h2>
          <SearchField
            ariaLabel={t("nav.searchLabel", {
              defaultValue: "Filter settings sections",
            })}
            clearLabel={t("nav.searchClear", { defaultValue: "Clear filter" })}
            compact
            onChange={setQuery}
            placeholder={t("nav.searchPlaceholder", {
              defaultValue: "Filter sections",
            })}
            value={query}
          />
          {matches.length === 0 ? (
            <div className="empty-state">
              <strong>
                {t("nav.noMatches", { defaultValue: "No section matches that" })}
              </strong>
              <p>
                {t("nav.noMatchesHint", {
                  defaultValue: "Clear the filter to see every section.",
                })}
              </p>
            </div>
          ) : (
            <div className="settings-section-picker-groups">
              {groups.map((group) => {
                const groupSections = matches.filter(
                  (section) => section.group === group.id,
                );
                if (groupSections.length === 0) {
                  return null;
                }
                const headingId = `settings-section-picker-group-${group.id}`;
                return (
                  <div className="settings-subnav-group" key={group.id}>
                    <p className="settings-subnav-group-title" id={headingId}>
                      {group.label}
                    </p>
                    <div
                      aria-labelledby={headingId}
                      className="settings-subnav-group-items"
                      role="group"
                    >
                      {groupSections.map((section) => (
                        <button
                          aria-current={
                            activeSection === section.id ? "page" : undefined
                          }
                          className={
                            activeSection === section.id ? "active" : ""
                          }
                          key={section.id}
                          onClick={() => choose(section.id, section.matchedTab)}
                          type="button"
                        >
                          <span className="settings-subnav-title">
                            <section.icon
                              aria-hidden="true"
                              className="settings-subnav-icon"
                              size={15}
                            />
                            <span className="settings-subnav-label">
                              {section.label}
                            </span>
                            {section.preview ? (
                              <span className="nav-preview-badge">
                                {section.preview.label}
                              </span>
                            ) : null}
                          </span>
                          {section.adminOnly ? (
                            <span className="nav-admin-badge">
                              {t("sectionGroups.adminChip", {
                                defaultValue: "System admin",
                              })}
                            </span>
                          ) : null}
                          {section.badge ? (
                            <span title={section.badgeTitle}>
                              <CountChip compact value={section.badge} />
                            </span>
                          ) : null}
                          {/* Unlike the desktop rail, the sheet shows every
                              description: it is the only place a narrow-screen
                              reader can learn what a section holds, and the
                              `title` tooltip the rail falls back to is
                              unreachable by touch. */}
                          <span className="meta-line">
                            {/* When the row is here because a tab matched,
                                say which -- otherwise "Content policies"
                                looks like an unrelated result for "lint". */}
                            {section.matchedTabLabel
                              ? t("nav.matchedTab", {
                                  defaultValue: "Goes to {{tab}}",
                                  tab: section.matchedTabLabel,
                                })
                              : section.description}
                          </span>
                          {activeSection === section.id ? (
                            <Check
                              aria-hidden="true"
                              className="settings-section-picker-check"
                              size={16}
                            />
                          ) : null}
                        </button>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Dialog>
      ) : null}
    </div>
  );
}

export type SettingsDestinationMatch = SettingsDestination & {
  /**
   * The policy tab whose label is why this row matched. Selecting the row then
   * deep-links to that tab rather than to the destination's default one.
   */
  matchedTab?: SettingsPolicyTab;
  /** The tab label to show under the row, so the match is not mysterious. */
  matchedTabLabel?: string;
};

/**
 * Match a destination on its own label and description, and on the labels of
 * the tabs it contains.
 *
 * Deliberately not a search over the `settings` namespace: policy field labels
 * are computed at render from the live policy JSON (`labelledPolicyFields`
 * walks `Object.keys(policy).sort()`), so there is no static table to index.
 * The searchable set is destination labels, descriptions and policy tab
 * labels, rather than every string in the settings namespace.
 *
 * The tab arm is the half that earns its keep: the Content policies merge put
 * five surfaces behind one entry, so without it typing "lint" from anywhere
 * matched nothing at all.
 */
export function filterSettingsDestinations(
  sections: SettingsDestination[],
  query: string,
  policyTabs: Array<{ key: SettingsPolicyTab; label: string }> = [],
): SettingsDestinationMatch[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) {
    return sections;
  }
  const matches: SettingsDestinationMatch[] = [];
  for (const section of sections) {
    // A tab-label hit wins over a description hit, and the order matters: the
    // Content policies description ends "...review routing, and lint", so
    // checking the description first meant typing "lint" matched the
    // destination and then opened Library. Landing on the tab you named is
    // the entire point of searching for it.
    const tab =
      section.id === "policies"
        ? policyTabs.find((entry) =>
            entry.label.toLocaleLowerCase().includes(needle),
          )
        : undefined;
    if (tab) {
      matches.push({
        ...section,
        matchedTab: tab.key,
        matchedTabLabel: tab.label,
      });
      continue;
    }
    if (
      `${section.label} ${section.description}`
        .toLocaleLowerCase()
        .includes(needle)
    ) {
      matches.push(section);
    }
  }
  return matches;
}
