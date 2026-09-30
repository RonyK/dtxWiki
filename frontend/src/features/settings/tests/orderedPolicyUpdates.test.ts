import { describe, expect, it } from "vitest";

import { orderedPolicyUpdates } from "../SettingsScreen";

// Review policy fields are written one PATCH at a time and a failure partway
// through leaves the earlier writes applied. The window between two writes is
// long enough for an auto-approval sweep to run, so the order decides what a
// half-applied save can do.
describe("orderedPolicyUpdates: review", () => {
  const allowed = (value: boolean) => ({
    field: {
      key: "automation.auto_approve_by_risk.low.allowed",
      label: "Low",
      rawValue: !value,
      value: String(value),
    },
    value,
  });
  const delay = (value: number) => ({
    field: {
      key: "automation.auto_approve_by_risk.low.delay_days",
      label: "Low",
      rawValue: 7,
      value: String(value),
    },
    value,
  });
  const keyOf = (update: { field: { key: string } }) =>
    update.field.key.split(".").pop();

  it("enables auto-approval only after its delay is stored", () => {
    // Otherwise a rejected delay leaves the level approving on a wait the user
    // never agreed to.
    const ordered = orderedPolicyUpdates("review", [allowed(true), delay(3)]);
    expect(ordered.map(keyOf)).toEqual(["delay_days", "allowed"]);
  });

  it("disables auto-approval before touching its delay", () => {
    // The reverse of the case above: writing delay_days=0 first would leave the
    // level approving immediately until the second write lands — and forever if
    // that write fails.
    const ordered = orderedPolicyUpdates("review", [delay(0), allowed(false)]);
    expect(ordered.map(keyOf)).toEqual(["allowed", "delay_days"]);
  });

  it("leaves unrelated groups in the caller's order", () => {
    const updates = [delay(1), allowed(true)];
    expect(orderedPolicyUpdates("workspace", updates)).toEqual(updates);
  });
});
