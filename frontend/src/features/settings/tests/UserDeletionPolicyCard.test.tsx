import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import { UserDeletionPolicyCard } from "../UserDeletionPolicyCard";

beforeEach(() => {
  vi.restoreAllMocks();
  setLocale("en", { persist: false });
});

it("reauthenticates before saving a zero-day deletion policy", async () => {
  const mutations: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input.toString();
    if (url.endsWith("/api/v1/admin/user-deletion-policy") && init?.method === "PATCH") {
      mutations.push("policy");
      expect(JSON.parse(String(init.body))).toEqual({ retention_days: 0 });
      return json({ data: { retention_days: 0 } });
    }
    if (url.endsWith("/api/v1/admin/user-deletion-policy")) return json({ data: { retention_days: 30 } });
    if (url.endsWith("/api/v1/auth/reauthenticate")) {
      mutations.push("reauthenticate");
      expect(JSON.parse(String(init?.body))).toEqual({ current_password: "admin-password" });
      return json({ data: { reauthenticated_until: "2026-10-27T00:10:00Z" } });
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  const user = userEvent.setup();
  render(<UserDeletionPolicyCard />);
  const days = await screen.findByLabelText("Retention period (days)");
  await user.clear(days);
  await user.type(days, "0");
  await user.type(screen.getByLabelText("Your current password"), "admin-password");
  await user.click(screen.getByRole("button", { name: "Save retention period" }));
  expect(await screen.findByText("Account retention period saved.")).toBeInTheDocument();
  expect(mutations).toEqual(["reauthenticate", "policy"]);
  expect(screen.getByLabelText("Your current password")).toHaveValue("");
});

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}
