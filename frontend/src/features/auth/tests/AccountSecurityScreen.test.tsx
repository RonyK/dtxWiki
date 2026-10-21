import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../shared/auth/AuthContext";
import {
  AccountPasswordScreen,
  AccountSecurityScreen,
} from "../AccountSecurityScreen";

const BASE = "http://127.0.0.1:21080";

describe("AccountSecurityScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
  });

  it("loads account data with profile actions under account information", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse({ data: userPayload("user@example.com") });
      }
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: {
            current_workspace_id: "workspace-1",
            workspaces: [
              {
                id: "workspace-1",
                slug: "default",
                title: "Default Workspace",
                name: "Default Workspace",
                goal: "Make wiki updates",
                default_language: "en",
                visibility: "private",
                role: "owner",
              },
            ],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) {
        return jsonResponse({
          data: [
            {
              id: "token-1",
              name: "Laptop MCP",
              scopes: ["mcp:read"],
              created_at: "2026-05-14T00:00:00Z",
              last_used_at: null,
              expires_at: null,
              revoked_at: null,
            },
          ],
        });
      }
      if (url === `${BASE}/oauth/consents`) {
        return jsonResponse({
          data: [
            {
              client_id: "client-1",
              client_name: "Claude Desktop",
              scope: "mcp:read",
              workspace_id: "workspace-1",
              workspace_title: "Default Workspace",
              workspace_slug: "default",
              granted_at: "2026-05-14T00:00:00Z",
            },
          ],
        });
      }
      if (url === `${BASE}/api/v1/auth/logout`) {
        expect(init?.credentials).toBe("include");
        return jsonResponse({ data: { logged_out: true } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/account"]}>
        <AuthProvider>
          <AccountSecurityScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("user@example.com")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Account" })).toBeInTheDocument();
    expect(screen.queryByText("00000000-0000-0000-0000-000000000010")).not.toBeInTheDocument();
    expect(screen.queryByText("Status")).not.toBeInTheDocument();
    expect(screen.queryByText("active")).not.toBeInTheDocument();
    expect(screen.getByText("Workspace access")).toBeInTheDocument();
    expect(screen.getByLabelText("Workspace access")).toHaveClass("workspace-access-panel");
    expect(await screen.findByText("Default Workspace")).toBeInTheDocument();
    // L-15 (260908 UI audit): the raw locale code ("EN") used to leak here
    // while Settings' language picker and the topbar toggle both spell it
    // out -- this now shares that same wording.
    expect(screen.getByText("English · Private")).toBeInTheDocument();
    expect(screen.queryByText(/^EN ·/)).not.toBeInTheDocument();
    // The current workspace shows both the "current" marker and the user's role
    // (L-f: the "current" pill no longer hides the role). The status pill now
    // routes through the shared i18n status map, so it reads "Current".
    expect(screen.getByText("Current")).toBeInTheDocument();
    expect(screen.getByText("Owner")).toBeInTheDocument();
    // Singular/plural pills stay grammatical ("1 workspace", not "1 workspaces").
    expect(screen.getByText("1 workspace")).toBeInTheDocument();
    expect(screen.queryByText("1 workspaces")).not.toBeInTheDocument();
    // The panel total was dropped: the two tiles below it already carry their
    // own counts, so it stated the same number a third time.
    expect(screen.queryByText("2 connections")).toBeNull();
    expect(screen.getByText("Connected tools")).toBeInTheDocument();
    expect(screen.getByText("MCP keys")).toBeInTheDocument();
    expect(screen.getByText("OAuth apps")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /MCP keys/ })).toHaveAttribute(
      "href",
      "/settings/integrations#integrations",
    );
    expect(screen.getByRole("link", { name: /OAuth apps/ })).toHaveAttribute(
      "href",
      "/settings/integrations#oauth-consents",
    );
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Update password" })).toHaveAttribute(
      "href",
      "/account/password",
    );

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/auth/logout`,
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("confirms the new password before submitting the password change", async () => {
    const passwordRequests: unknown[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse({ data: userPayload("user@example.com") });
      }
      if (url === `${BASE}/api/v1/auth/password`) {
        expect(init?.credentials).toBe("include");
        passwordRequests.push(JSON.parse(String(init?.body)));
        return jsonResponse({ data: sessionPayload("user@example.com") });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/account/password"]}>
        <AuthProvider>
          <AccountPasswordScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Update password" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Current password"), "old-password-123");
    await user.type(screen.getByLabelText("New password"), "new-password-123");
    await user.type(screen.getByLabelText("Confirm new password"), "different-password");
    await user.click(screen.getByRole("button", { name: "Update password" }));

    expect(await screen.findByText("New passwords do not match.")).toBeInTheDocument();
    expect(passwordRequests).toHaveLength(0);

    await user.clear(screen.getByLabelText("Confirm new password"));
    await user.type(screen.getByLabelText("Confirm new password"), "new-password-123");
    await user.click(screen.getByRole("button", { name: "Update password" }));

    expect(await screen.findByText("Password updated. Returning to account…"))
      .toBeInTheDocument();
    expect(passwordRequests).toEqual([
      {
        current_password: "old-password-123",
        new_password: "new-password-123",
      },
    ]);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/auth/password`,
      expect.objectContaining({ method: "POST" }),
    );
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/account");
    }, { timeout: 2500 });
  });

  it("lets an unverified user request a verification email", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse({ data: userPayload("user@example.com", null) });
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse({
          data: {
            signup_enabled: false,
            password_recovery: { mode: "email", admin_assisted: false },
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: null, default_workspace_id: null, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens` || url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/auth/email-verification/request`) {
        expect(init?.method).toBe("POST");
        expect(init?.credentials).toBe("include");
        return jsonResponse({ data: { requested: true, verified: false } }, 202);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const actor = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/account"]}>
        <AuthProvider>
          <AccountSecurityScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Email not verified")).toBeInTheDocument();
    await actor.click(await screen.findByRole("button", { name: "Verify email" }));
    expect(
      await screen.findByText("Verification instructions were sent to your email."),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/auth/email-verification/request`,
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("shows the localized message, not the raw server string, when verification 422s (UI-12)", async () => {
    // Regression guard for safe API error mapping: the untranslated
    // body.message ("email verification is not configured") must not render
    // instead of this component's own copy. capabilities say mode=email (so
    // the button renders and the
    // request is attempted), but the request itself still 422s -- e.g. a
    // race where SMTP dropped between the capability check and the click.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse({ data: userPayload("user@example.com", null) });
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse({
          data: {
            signup_enabled: false,
            password_recovery: { mode: "email", admin_assisted: false },
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: null, default_workspace_id: null, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens` || url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/auth/email-verification/request`) {
        return jsonResponse(
          {
            error_code: "password_recovery_unavailable",
            message: "email verification is not configured",
            request_id: "",
            warnings: [],
          },
          422,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const actor = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/account"]}>
        <AuthProvider>
          <AccountSecurityScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    await actor.click(await screen.findByRole("button", { name: "Verify email" }));

    expect(
      await screen.findByText(
        "Email verification is not available on this server. Contact the server operator.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("email verification is not configured"),
    ).not.toBeInTheDocument();
  });

  it("does not over-match a different failure to the same canned copy (UI-12)", async () => {
    // The isApiError gate must be specific to (422, password_recovery_unavailable)
    // -- a different, already-mapped error code (auth_failed) must keep
    // showing its own message via apiErrorMessage, not get swallowed into
    // the email-verification copy just because it happened during the same
    // request.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse({ data: userPayload("user@example.com", null) });
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse({
          data: {
            signup_enabled: false,
            password_recovery: { mode: "email", admin_assisted: false },
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: null, default_workspace_id: null, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens` || url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/auth/email-verification/request`) {
        return jsonResponse(
          {
            error_code: "auth_failed",
            message: "raw-server-detail-must-not-render",
            request_id: "",
            warnings: [],
          },
          401,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const actor = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/account"]}>
        <AuthProvider>
          <AccountSecurityScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    await actor.click(await screen.findByRole("button", { name: "Verify email" }));

    expect(
      await screen.findByText("Authentication failed. Try again."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "Email verification is not available on this server. Contact the server operator.",
      ),
    ).not.toBeInTheDocument();
  });

  it("says nothing at all about email verification on a server with no mail provider", async () => {
    // Supersedes the earlier UI-05/UI-12 contract, which replaced the
    // dead-end button with an operator-contact hint. That still told someone
    // their email was unverified and pointed them at an operator, about a
    // capability the server never had -- a warning with no action behind it.
    // On a mail-less install email verification is not a thing that exists,
    // so the whole row is absent rather than present-and-apologetic.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse({ data: userPayload("user@example.com", null) });
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse({
          data: {
            signup_enabled: false,
            password_recovery: { mode: "admin_assisted", admin_assisted: true },
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: null, default_workspace_id: null, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens` || url === `${BASE}/oauth/consents`) {
        return jsonResponse({ data: [] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/account"]}>
        <AuthProvider>
          <AccountSecurityScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    // Wait for something that IS on the page before asserting absences, so
    // this cannot pass merely by running before the screen rendered.
    expect(await screen.findByText("user@example.com")).toBeInTheDocument();

    expect(screen.queryByText("Email not verified")).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "Email verification is not available on this server. Contact the server operator.",
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Verify email" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the verified pill when the server never reports a recovery mode (UI-03)", async () => {
    // The row used to be gated solely on emailVerificationOffered, which is
    // derived from passwordRecoveryMode. Nothing loads capabilities for an
    // already-verified account -- AccountSecurityScreen early-returns on
    // email_verified_at, and App only auto-loads them on auth-bootstrap
    // routes -- so the mode stays at its "disabled" default and the pill
    // disappeared on EVERY server, mail-configured ones included. Being
    // verified is a fact about the account, not a capability of the server.
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/auth/me`) {
          return jsonResponse({ data: userPayload("verified@example.com") });
        }
        if (url === `${BASE}/api/v1/workspaces`) {
          return jsonResponse({
            data: {
              current_workspace_id: null,
              default_workspace_id: null,
              workspaces: [],
            },
          });
        }
        if (url === `${BASE}/api/v1/auth/tokens` || url === `${BASE}/oauth/consents`) {
          return jsonResponse({ data: [] });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    render(
      <MemoryRouter initialEntries={["/account"]}>
        <AuthProvider>
          <AccountSecurityScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("verified@example.com")).toBeInTheDocument();
    expect(await screen.findByText("Email verified")).toBeInTheDocument();

    // The pill is a statement, not an invitation: nothing to verify, and no
    // "not verified" warning either.
    expect(
      screen.queryByRole("button", { name: "Verify email" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Email not verified")).not.toBeInTheDocument();

    // Pins the reason the old gate failed: this screen never asks for
    // capabilities on a verified account, so the pill must not depend on them.
    expect(
      fetchMock.mock.calls.some((call) =>
        call[0].toString().includes("/api/v1/auth/capabilities"),
      ),
    ).toBe(false);
  });
});

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname}</span>;
}

function sessionPayload(email: string) {
  return {
    user: userPayload(email),
    expires_at: "2026-05-14T12:00:00Z",
  };
}

function userPayload(
  email: string,
  emailVerifiedAt: string | null = "2026-05-14T00:00:00Z",
) {
  return {
    user_id: "00000000-0000-0000-0000-000000000010",
    email,
    display_name: "User",
    status: "active",
    email_verified_at: emailVerifiedAt,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
