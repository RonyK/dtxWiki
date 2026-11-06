import type { TFunction } from "i18next";

import type { LintFinding } from "../../shared/api/operations";

export function lintRuleKey(ruleId: string): string {
  return ruleId.replaceAll(".", "_");
}

export function lintRuleLabel(t: TFunction, ruleId: string): string {
  return t(`rules.${lintRuleKey(ruleId)}.label`, {
    defaultValue: humanizeRule(ruleId),
  });
}

/**
 * One sentence saying what a rule checks, for surfaces that list rules rather
 * than findings (Settings > Lint).
 *
 * Deliberately NOT the rule's finding `message`: that sentence is written about
 * a page that already failed ("A fenced code block is not closed"), so reusing
 * it on a settings screen tells the reader their workspace has a problem it has
 * not been checked for. A rule with no `description` renders without one.
 */
export function lintRuleDescription(t: TFunction, ruleId: string): string {
  return t(`rules.${lintRuleKey(ruleId)}.description`, { defaultValue: "" });
}

/**
 * Interpolation values for a finding's message: its stored arguments plus the
 * read-time titles of its target and of the other page it is about, so a pair
 * finding can name both pages ("{{target_title}} and {{related_title}} ...").
 */
function lintMessageArgs(t: TFunction, finding: LintFinding) {
  return {
    ...(finding.message_args ?? {}),
    target_title:
      finding.target_title || t(`targetType.${finding.target_type}`),
    related_title:
      finding.related_targets?.[0]?.title || t("relatedTargetFallback"),
  };
}

export function lintFindingMessage(t: TFunction, finding: LintFinding): string {
  const messageKey = finding.message_key || finding.rule_id;
  return t(`rules.${lintRuleKey(messageKey)}.message`, {
    ...lintMessageArgs(t, finding),
    defaultValue: t("findingFallback", {
      target: finding.target_title || t(`targetType.${finding.target_type}`),
    }),
  });
}

/**
 * Whether the translated message actually names this finding's specifics.
 *
 * Some rules interpolate ("The wiki link [[{{written_slug}}]] does not
 * resolve"); the format rules state only the rule's general condition ("A
 * heading skips one or more levels") and keep the exact location — the line
 * number and the offending text — in `suggested_action`.
 *
 * Presence of `message_args` cannot decide this: every finding carries a
 * `body_scopes` entry that no message template consumes. So render the message
 * with and without the arguments and see whether they made a difference.
 */
export function lintMessageIsSpecific(
  t: TFunction,
  finding: LintFinding,
): boolean {
  const messageKey = finding.message_key || finding.rule_id;
  const path = `rules.${lintRuleKey(messageKey)}.message`;
  const withArgs = t(path, {
    ...lintMessageArgs(t, finding),
    defaultValue: "",
  });
  return withArgs !== "" && withArgs !== t(path, { defaultValue: "" });
}

function humanizeRule(ruleId: string): string {
  const tail = ruleId.includes(".") ? ruleId.slice(ruleId.indexOf(".") + 1) : ruleId;
  return tail.replaceAll("_", " ");
}
