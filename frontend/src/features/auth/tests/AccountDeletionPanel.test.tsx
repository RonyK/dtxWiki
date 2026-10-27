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

it("waits for shared workspaces and the confirmed retention period before withdrawal", async () => {
  let retentionDays = 30;
  let blocking = [{ workspace_id: "ws-team", title: "Team handbook", other_member_count: 2 }];
  const requests: unknown[] = [];
  const open = vi.spyOn(window, "open").mockReturnValue(null);
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input.toString();
    if (url.endsWith("/api/v1/users/me/deletion-preview")) {
      return json({
        data: {
          retention_days: retentionDays,
          target_workspaces: [{ workspace_id: "ws-personal", title: "My notes", other_member_count: 0 }],
          blocking_workspaces: blocking,
        },
      });
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
  expect(screen.getByText("My notes")).toBeInTheDocument();
  expect(screen.getByText("Team handbook (2 other members)")).toBeInTheDocument();
  const submit = screen.getByRole("button", { name: "Confirm account deletion" });
  // The server normalizes the address, so a different case still confirms.
  await user.type(screen.getByLabelText("Type user@example.com to confirm"), " User@Example.com ");
  await user.type(screen.getByLabelText("Current password for account deletion"), "current-password");
  // A workspace that still has other members must be handled in its own
  // Settings first, opened beside this form.
  expect(submit).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Open workspace Settings" }));
  expect(open).toHaveBeenCalledWith(
    "/workspaces/ws-team/settings?section=workspace#workspace-danger-title",
    "_blank",
    "noopener,noreferrer",
  );
  blocking = [];
  await user.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(screen.queryByText(/Team handbook/)).not.toBeInTheDocument());
  await user.click(submit);
  expect(await screen.findByText("The retention period changed. Reload the period and review it before confirming again.")).toBeInTheDocument();
  expect(signOut).not.toHaveBeenCalled();
  expect(screen.getByLabelText("Current password for account deletion")).toHaveValue("");
  await user.click(screen.getByRole("button", { name: "Check again" }));
  expect(await screen.findByText(/retained for 0 days/)).toBeInTheDocument();
  await user.type(screen.getByLabelText("Current password for account deletion"), "current-password");
  await user.click(submit);
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/login"));
  expect(signOut).toHaveBeenCalledOnce();
  const approved = [{ workspace_id: "ws-personal", other_member_count: 0 }];
  expect(requests).toEqual([
    { confirmation_email: "User@Example.com", current_password: "current-password", expected_retention_days: 30, approved_workspaces: approved },
    { confirmation_email: "User@Example.com", current_password: "current-password", expected_retention_days: 0, approved_workspaces: approved },
  ]);
});

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
