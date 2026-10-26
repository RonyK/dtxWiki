import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import { AccountDeletionPanel } from "../AccountDeletionPanel";

const { signOut } = vi.hoisted(() => ({ signOut: vi.fn() }));
vi.mock("../../../shared/auth/AuthContext", () => ({ useAuth: () => ({ signOut }) }));

beforeEach(() => {
  vi.restoreAllMocks();
  signOut.mockReset().mockResolvedValue(undefined);
  setLocale("en", { persist: false });
});

it("requires the confirmed retention period again when policy changes before withdrawal", async () => {
  let retentionDays = 30;
  const requests: unknown[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input.toString();
    if (url.endsWith("/api/v1/users/me/deletion-policy")) {
      return json({ data: { retention_days: retentionDays } });
    }
    if (url.endsWith("/api/v1/users/me") && init?.method === "DELETE") {
      expect(init.credentials).toBe("include");
      requests.push(JSON.parse(String(init.body)));
      if (requests.length === 1) {
        retentionDays = 0;
        return json({ error_code: "user_deletion_policy_changed", message: "policy changed", request_id: "test", warnings: [] }, 409);
      }
      return json({ data: { user_id: "user-1", status: "deletion_pending", requested_at: "2026-10-27T00:00:00Z", purge_after: "2026-10-27T00:00:00Z", retention_days: 0 } });
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/account"]}><AccountDeletionPanel email="user@example.com" /><LocationProbe /></MemoryRouter>);
  expect(globalThis.fetch).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Review account deletion" }));
  expect(await screen.findByText(/retained for 30 days/)).toBeInTheDocument();
  const submit = screen.getByRole("button", { name: "Confirm account deletion" });
  expect(submit).toBeDisabled();
  await user.type(screen.getByLabelText("Type user@example.com to confirm"), "user@example.com");
  await user.type(screen.getByLabelText("Current password for account deletion"), "current-password");
  await user.click(submit);
  expect(await screen.findByText("The retention period changed. Reload the period and review it before confirming again.")).toBeInTheDocument();
  expect(signOut).not.toHaveBeenCalled();
  expect(screen.getByLabelText("Current password for account deletion")).toHaveValue("");
  await user.click(screen.getByRole("button", { name: "Reload retention period" }));
  expect(await screen.findByText(/retained for 0 days/)).toBeInTheDocument();
  await user.type(screen.getByLabelText("Current password for account deletion"), "current-password");
  await user.click(submit);
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/login"));
  expect(signOut).toHaveBeenCalledOnce();
  expect(requests).toEqual([
    { confirmation_email: "user@example.com", current_password: "current-password", expected_retention_days: 30 },
    { confirmation_email: "user@example.com", current_password: "current-password", expected_retention_days: 0 },
  ]);
});

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
