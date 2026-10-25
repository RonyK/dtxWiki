import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import { SettingsScreen } from "../SettingsScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";

describe("Settings account recovery surfaces", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    setLocale("en", { persist: false });
  });

  it("shows the User accounts tab and recovery surface only to a Server Admin", async () => {
    mockSettingsFetch({ isServerAdmin: true });
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/settings?section=users`]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("button", { name: /User accounts/ }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Account recovery" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Server admin only")).toBeInTheDocument();
  });

  it("hides the User accounts tab and gates a direct link for a non-admin", async () => {
    mockSettingsFetch({ isServerAdmin: false });
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/settings?section=users`]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "User search and account recovery are visible to server admins only.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /User accounts/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Account recovery" }),
    ).not.toBeInTheDocument();
  });

  it("renders Identity & Recovery status and redacts SMTP credentials in commands", async () => {
    const user = userEvent.setup();
    mockSettingsFetch({
      isServerAdmin: true,
      deployment: deploymentResponse(),
    });
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/settings?section=deployment`]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const heading = await screen.findByRole("heading", {
      name: "Password recovery",
    });
    const card = heading.closest("section");
    expect(card).not.toBeNull();
    const scoped = within(card as HTMLElement);
    expect(scoped.getByText("Identity & Recovery")).toBeInTheDocument();
    expect(scoped.getAllByText("Email")).not.toHaveLength(0);
    expect(scoped.getByText("Configured (value hidden)")).toBeInTheDocument();
    expect(scoped.getByText("Last delivery failed")).toBeInTheDocument();
    expect(
      card?.querySelector('time[datetime="2026-07-31T03:15:00Z"]'),
    ).not.toBeNull();
    expect(scoped.getByText("3 queued · 1 failed")).toBeInTheDocument();
    expect(screen.queryByText(/real-smtp-secret/)).not.toBeInTheDocument();

    const disclosure = scoped.getByRole("button", {
      name: "Show setup method",
    });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    await user.click(disclosure);

    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByText(/real-smtp-secret/)).not.toBeInTheDocument();
    expect(
      scoped.getAllByText(/DTXWIKI_SMTP_PASSWORD='<set directly in terminal>'/),
    ).toHaveLength(2);
    expect(scoped.getAllByText("Systemd installation")).toHaveLength(2);
    expect(scoped.getAllByText("Docker Compose installation")).toHaveLength(2);
  });
});

function mockSettingsFetch({
  deployment,
  isServerAdmin,
}: {
  deployment?: unknown;
  isServerAdmin: boolean;
}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    if (url === `${BASE}/api/v1/workspaces`) {
      return jsonResponse({
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            {
              id: WORKSPACE_ID,
              slug: "default",
              title: "Default Workspace",
              role: "owner",
              is_default: true,
            },
          ],
        },
      });
    }
    if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
      return jsonResponse({
        data: {
          workspace: policy("workspace"),
          library: policy("library"),
          citation: policy("citation"),
          context_write: policy("context_write"),
          agent_injection: policy("agent_injection"),
          review: policy("review"),
          lint: policy("lint"),
          mcp_clients: [],
        },
      });
    }
    if (url === `${BASE}/api/v1/auth/tokens`) {
      return jsonResponse({ data: [] });
    }
    if (url === `${BASE}/oauth/consents`) {
      return jsonResponse({ data: [] });
    }
    if (url === `${BASE}/api/v1/auth/me`) {
      return jsonResponse({
        data: {
          user_id: "00000000-0000-0000-0000-000000000002",
          email: "admin@example.com",
          display_name: "Admin",
          status: "active",
          is_server_admin: isServerAdmin,
          oauth_available: true,
          oauth_unavailable_reason: null,
        },
      });
    }
    if (url === `${BASE}/api/v1/auth/capabilities`) {
      return jsonResponse({
        data: {
          signup_enabled: false,
          password_recovery: {
            mode: "email",
            admin_assisted: true,
          },
        },
      });
    }
    if (url === `${BASE}/api/v1/admin/deployment` && deployment) {
      return jsonResponse(deployment);
    }
    throw new Error(`Unexpected request: ${url}`);
  });
}

function policy(group: string) {
  return {
    group,
    title: `${group} policy`,
    policy: {},
    decision: "allow",
  };
}

function deploymentResponse() {
  return {
    data: {
      public_base_url: "https://wiki.example.com",
      mcp_url: "https://wiki.example.com/mcp",
      app_mode: "team",
      app_env: "production",
      is_production: true,
      behind_proxy: true,
      https_configured: true,
      tls_mode: "acme",
      oauth_available: true,
      oauth_unavailable_reason: null,
      health_ready: true,
      health_components: {
        db: "ok",
        app_version: "ok",
        mcp: "ok",
        oauth_jwk: "ok",
      },
      env_file_hint: "/etc/dtxwiki/dtxwiki.env",
      signup: {
        configured: false,
        effective: false,
        source: "explicit_environment",
        mode_defaults: {
          solo_local: true,
          solo_remote: false,
          team: false,
        },
        restart_required: true,
        impact: "new_signups_only",
        systemd_command: "sudo dtxwiki config set DTXWIKI_SIGNUP_ENABLED=true",
        compose_command: "DTXWIKI_SIGNUP_ENABLED=true",
      },
      recovery: {
        mode: "email",
        admin_assisted: true,
        link_delivery_available: true,
        mail_provider: "smtp",
        mail_status: "configured",
        mail_connection_status: "failed",
        mail_last_attempt_at: "2026-07-31T03:15:00Z",
        mail_from_address: "wiki@example.com",
        smtp_credentials_configured: true,
        canonical_public_url: "https://wiki.example.com",
        https_ready: true,
        outbox_queued: 3,
        outbox_failed: 1,
        worker_required: true,
        restart_required: true,
        systemd_command:
          "sudo dtxwiki config set DTXWIKI_SMTP_PASSWORD='real-smtp-secret' --i-understand",
        compose_command: "DTXWIKI_SMTP_PASSWORD=real-smtp-secret",
      },
      suggested_commands: [],
      current_version: "1.0.0",
      latest_version: null,
      update_available: false,
    },
  };
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
