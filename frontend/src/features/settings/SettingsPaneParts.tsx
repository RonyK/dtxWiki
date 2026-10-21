import type { ReactNode, Ref } from "react";

import { LoadingIndicator } from "../../shared/components/LoadingIndicator";
import { StatusPill } from "../../shared/components/StatusPill";

/**
 * The three shapes every Settings pane kept re-deriving.
 *
 * Before this, `.admin-jobs-head`, `.attachments-head` and
 * `.admin-user-recovery-head` were the same markup behind three class names
 * with three identical CSS blocks (`display:flex; gap:16px;
 * align-items:flex-start; justify-content:space-between`) plus three identical
 * `flex-direction: column` overrides in the narrow query -- measured identical
 * on the running app, not merely similar in source.
 *
 * Loading was worse: five presentations (`RouteLoading`, `.meta-line`,
 * `.field-hint`, `.lint-loading`, and a *loading* message rendered inside
 * `.empty-state`) with `role="status"` on exactly one of them, so a screen
 * reader was told the pane was loading in one place out of five.
 */

export function SettingsPaneHeader({
  action,
  description,
  eyebrow,
  headingId,
  headingRef,
  title,
}: {
  /** Right-aligned control (Refresh, a scope chip). */
  action?: ReactNode;
  description?: ReactNode;
  /** Scope line above the title. Omit where the page header already says it. */
  eyebrow?: ReactNode;
  headingId: string;
  /** For panes that move focus here after a destructive action. */
  headingRef?: Ref<HTMLHeadingElement>;
  title: ReactNode;
}) {
  return (
    <div className="settings-pane-head">
      <div>
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <h2
          id={headingId}
          ref={headingRef}
          // Panes that focus their heading need the -1; giving it to all of
          // them costs nothing and keeps the contract in one place.
          tabIndex={-1}
        >
          {title}
        </h2>
        {description ? <p className="feature-subtitle">{description}</p> : null}
      </div>
      {action ?? null}
    </div>
  );
}

/** Settings-specific spacing around the shared, announced loading indicator. */
export function SettingsLoadingRow({ children }: { children: ReactNode }) {
  return (
    <LoadingIndicator as="p" className="settings-loading-row">
      {children}
    </LoadingIndicator>
  );
}

/**
 * One empty state: a title and a sentence saying what would fill it.
 *
 * The bare-text variant this replaces rendered a single sentence with no
 * heading, and in one case rendered a *loading* message inside it -- an empty
 * state that was not empty and not a state.
 */
export function SettingsEmptyState({
  className,
  description,
  title,
}: {
  className?: string;
  description?: ReactNode;
  title: ReactNode;
}) {
  return (
    <div className={className ? `empty-state ${className}` : "empty-state"}>
      <strong>{title}</strong>
      {description ? <p>{description}</p> : null}
    </div>
  );
}

/** The edit states a policy card can be in, worst-to-best for the reader. */
export type PolicyEditState =
  | "readOnly"
  | "ownerOnly"
  | "partiallyEditable"
  | "editable"
  | "unsaved";

/**
 * `StatusPill` matches its tone on the raw `status` string
 * (`StatusPill.tsx:73-76`), so every settings card that passed an
 * already-translated string got the `info` fallback: measured on the running
 * app, both the Library card's "Editable" chip and Lint's rendered
 * `status-pill-info`, which is the same neutral grey a read-only card gets.
 * In Korean nothing could ever match, so the tone was neutral by construction.
 *
 * One mapping, applied everywhere, and the displayed text stays localized
 * because it travels as `label` rather than as `status`.
 */
const POLICY_STATE_TONE: Record<PolicyEditState, string> = {
  readOnly: "muted",
  ownerOnly: "muted",
  partiallyEditable: "warn",
  editable: "ok",
  unsaved: "warn",
};

export function PolicyStateChip({
  label,
  state,
}: {
  label: string;
  state: PolicyEditState;
}) {
  return (
    <StatusPill
      compact
      label={label}
      status={state}
      // Without this the tooltip would fall back to the raw enum, which is the
      // English key -- the exact leak the localized label is here to stop.
      title={label}
      tone={POLICY_STATE_TONE[state]}
    />
  );
}
