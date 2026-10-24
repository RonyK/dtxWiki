import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import { SettingsScreen } from "../SettingsScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

function policyCard(name: string) {
  return { name, title: `${name} policy`, policy: {} };
}

const POLICIES = {
  workspace: policyCard("workspace"),
  library: policyCard("library"),
  citation: policyCard("citation"),
  context_write: policyCard("context_write"),
  agent_injection: policyCard("agent_injection"),
  review: policyCard("review"),
  mcp_clients: [],
};

const DEPLOYMENT = {
  public_base_url: "https://wiki.example.com",
  mcp_url: "https://wiki.example.com/mcp",
  app_mode: "team",
  app_env: "production",
  is_production: true,
  behind_proxy: true,
  https_configured: true,
  tls_mode: "acme",
  oauth_available: true,
  oauth_dcr_enabled: true,
  oauth_unavailable_reason: null,
  oauth_token_lifetimes: {
    access_token: { seconds: 3600, source: "environment override" },
    refresh_token: { seconds: 2592000, source: "built-in default" },
    refresh_sliding: { enabled: true, source: "built-in default" },
  },
  health_ready: false,
  health_components: {
    db: "ok",
    app_version: "ok",
    mcp: "ok",
    oauth_jwk: "ephemeral_unsafe",
  },
  env_file_hint: "/etc/dtxwiki/dtxwiki.env",
  signup: {
    configured: null,
    effective: false,
    source: "mode_default",
    mode_defaults: {
      solo_local: true,
      solo_remote: false,
      team: false,
    },
    restart_required: true,
    impact: "new_signups_only",
    systemd_command: "sudo dtxwiki config set DTXWIKI_SIGNUP_ENABLED=true",
    compose_command:
      "docker compose run --rm app dtxwiki config set DTXWIKI_SIGNUP_ENABLED=true",
  },
  recovery: {
    mode: "email",
    admin_assisted: true,
    link_delivery_available: true,
    mail_provider: "smtp",
    mail_status: "configured",
    mail_connection_status: "not_checked",
    mail_last_attempt_at: null,
    mail_from_address: "wiki@example.com",
    smtp_credentials_configured: true,
    canonical_public_url: "https://wiki.example.com",
    https_ready: true,
    outbox_queued: 0,
    outbox_failed: 0,
    worker_required: true,
    restart_required: true,
    systemd_command:
      "sudo dtxwiki config set DTXWIKI_MAIL_PROVIDER=smtp DTXWIKI_SMTP_PASSWORD=<secret>",
    compose_command:
      "docker compose run --rm app dtxwiki config set DTXWIKI_MAIL_PROVIDER=smtp DTXWIKI_SMTP_PASSWORD=<secret>",
  },
  suggested_commands: [
    {
      title: "Change domain",
      description: "Point the wiki at a new HTTPS domain.",
      command: "sudo dtxwiki config set DTXWIKI_PUBLIC_BASE_URL=https://new.example.com",
      blast_radius: "restart",
    },
  ],
  current_version: "0.1.0",
  latest_version: null,
  update_available: false,
  check_status: "succeeded",
  checked_at: "2026-10-23T00:00:00Z",
  stale: false,
  source: "pypi",
};

function mockFetch(role: string, deployment = DEPLOYMENT) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    if (url === `${BASE}/api/v1/workspaces`) {
      return json({
        data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            { id: WORKSPACE_ID, name: "Default", slug: "default", role },
          ],
        },
      });
    }
    if (url === `${BASE}/api/v1/auth/me`) {
      return json({
        data: {
          user_id: "00000000-0000-0000-0000-000000000001",
          email: "user@example.com",
          display_name: "User",
          status: "active",
          // Deployment access is instance server-admin (not workspace owner);
          // map the fixture's "owner" role to a server-admin for these tests.
          is_server_admin: role === "owner",
        },
      });
    }
    if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
      return json({ data: POLICIES });
    }
    if (url === `${BASE}/api/v1/admin/deployment`) {
      return json({ data: deployment });
    }
    if (url === `${BASE}/api/v1/auth/tokens`) {
      return json({ data: [] });
    }
    if (url === `${BASE}/oauth/consents`) {
      return json({ data: [] });
    }
    throw new Error(`Unexpected request: ${url}`);
  });
}

describe("Settings > Deployment tab", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    setLocale("en", { persist: false });
  });

  it("shows deployment status and a copyable reconfigure command for owners", async () => {
    mockFetch("owner");
    render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    // Owner sees the Deployment nav entry (named apart from the /setup wizard).
    expect(
      await screen.findByRole("button", { name: /Deployment/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Setup/i }),
    ).not.toBeInTheDocument();

    // Status surface renders the current public + MCP URLs.
    expect(
      (await screen.findAllByText("https://wiki.example.com")).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("https://wiki.example.com/mcp")).toBeInTheDocument();
    const instanceScope = screen.getByText(/these settings apply to the whole instance \(all workspaces\)/);
    const updateCard = screen.getByRole("heading", { name: "Version & updates" }).closest("section");
    expect(instanceScope).toBeInTheDocument();
    expect(updateCard).not.toContainElement(instanceScope);
    expect(screen.getByText("OAuth access lifetime")).toBeInTheDocument();
    expect(screen.getByText("OAuth client self-registration")).toBeInTheDocument();
    expect(screen.getByText("3600 seconds · Environment override")).toBeInTheDocument();
    expect(screen.getByText("30 days · Built-in default")).toBeInTheDocument();
    expect(screen.getByText("Enabled · Built-in default")).toBeInTheDocument();

    // Read-only guidance + a copyable config command are shown.
    expect(screen.getByText(/run the shown command in a terminal/i)).toBeInTheDocument();
    expect(screen.getAllByText("Change domain").length).toBeGreaterThan(0);
    expect(
      screen.getByText(
        "sudo dtxwiki config set DTXWIKI_PUBLIC_BASE_URL=https://new.example.com",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("/etc/dtxwiki/dtxwiki.env", { exact: false }))
      .toBeInTheDocument();
    const configurationCard = screen
      .getByRole("heading", { name: "Change configuration" })
      .closest(".deployment-configuration-card");
    expect(configurationCard).not.toBeNull();
    // Assert the visible heading specifically (the command title also renders in
    // the CommandBlock's CSS-hidden label span, so a length>0 check would pass
    // even if the visible <strong> were dropped).
    expect(
      within(configurationCard as HTMLElement).getByText("Change domain", {
        selector: ".deployment-command-head strong",
      }),
    ).toBeInTheDocument();
  });

  it("keeps URL-client-id OAuth distinct when deployment DCR is disabled", async () => {
    mockFetch("owner", { ...DEPLOYMENT, oauth_dcr_enabled: false });
    render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const row = (await screen.findByText("OAuth client self-registration")).closest(
      ".deployment-status-row",
    ) as HTMLElement;
    expect(within(row).getByText("Disabled")).toBeInTheDocument();
    expect(
      within(row).getByText(
        "OAuth with HTTPS URL client identifiers remains available.",
      ),
    ).toBeInTheDocument();
  });

  it("renders per-component readiness rows so a degraded probe names the cause", async () => {
    // M-E: health_ready=false with oauth_jwk=ephemeral_unsafe must surface the
    // failing component and its status, not just a bare "Degraded" pill.
    mockFetch("owner");
    const { container } = render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    const components = (await screen.findByText("OAuth signing key")).closest(
      ".deployment-health-components",
    ) as HTMLElement;
    expect(components).toBeInTheDocument();
    // The healthy components render their OK status...
    expect(within(components).getByText("Database")).toBeInTheDocument();
    expect(within(components).getByText("App version")).toBeInTheDocument();
    expect(within(components).getByText("MCP service")).toBeInTheDocument();
    // ...and the failing one names its cause with a danger-toned pill.
    const oauthRow = within(components)
      .getByText("OAuth signing key")
      .closest(".deployment-status-row") as HTMLElement;
    const pill = within(oauthRow).getByText("Ephemeral (unsafe)");
    expect(pill.closest(".status-pill")).toHaveClass("status-pill-danger");
    expect(container.querySelector(".deployment-health-components")).not.toBeNull();
  });

  it("hides per-component readiness rows when the probe is healthy", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (url === `${BASE}/api/v1/workspaces`) {
        return json({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              { id: WORKSPACE_ID, name: "Default", slug: "default", role: "owner" },
            ],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/me`)
        return json({
          data: {
            user_id: "00000000-0000-0000-0000-000000000001",
            email: "user@example.com",
            display_name: "User",
            status: "active",
            is_server_admin: true,
          },
        });
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
        return json({ data: POLICIES });
      }
      if (url === `${BASE}/api/v1/admin/deployment`) {
        return json({
          data: {
            ...DEPLOYMENT,
            health_ready: true,
            health_components: {
              db: "ok",
              app_version: "ok",
              mcp: "ok",
              oauth_jwk: "ok",
            },
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) return json({ data: [] });
      if (url === `${BASE}/oauth/consents`) return json({ data: [] });
      throw new Error(`Unexpected request: ${url}`);
    });

    const { container } = render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      (await screen.findAllByText("https://wiki.example.com")).length,
    ).toBeGreaterThan(0);
    expect(container.querySelector(".deployment-health-components")).toBeNull();
    expect(screen.queryByText("OAuth signing key")).not.toBeInTheDocument();
  });

  it("surfaces a TLS-not-ready warning when no https URL is configured", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (url === `${BASE}/api/v1/workspaces`) {
        return json({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              { id: WORKSPACE_ID, name: "Default", slug: "default", role: "owner" },
            ],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/me`)
        return json({
          data: {
            user_id: "00000000-0000-0000-0000-000000000001",
            email: "user@example.com",
            display_name: "User",
            status: "active",
            is_server_admin: true,
          },
        });
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
        return json({ data: POLICIES });
      }
      if (url === `${BASE}/api/v1/admin/deployment`) {
        return json({
          data: { ...DEPLOYMENT, public_base_url: null, mcp_url: null, https_configured: false },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) return json({ data: [] });
      if (url === `${BASE}/oauth/consents`) return json({ data: [] });
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(/HTTPS is not configured/i),
    ).toBeInTheDocument();
  });

  it("shows the release and installation selector without a generic updater command", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (url === `${BASE}/api/v1/workspaces`) {
        return json({
          data: {
            current_workspace_id: WORKSPACE_ID,
            workspaces: [
              { id: WORKSPACE_ID, name: "Default", slug: "default", role: "owner" },
            ],
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/me`)
        return json({
          data: {
            user_id: "00000000-0000-0000-0000-000000000001",
            email: "user@example.com",
            display_name: "User",
            status: "active",
            is_server_admin: true,
          },
        });
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) {
        return json({ data: POLICIES });
      }
      if (url === `${BASE}/api/v1/admin/deployment`) {
        return json({
          data: {
            ...DEPLOYMENT,
            update_available: true,
            current_version: "0.1.0",
            latest_version: "0.2.0",
          },
        });
      }
      if (url === `${BASE}/api/v1/auth/tokens`) return json({ data: [] });
      if (url === `${BASE}/oauth/consents`) return json({ data: [] });
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("New release")).toBeInTheDocument();
    expect(screen.getByText("0.2.0")).toBeInTheDocument();
    const selector = screen.getByRole("combobox", {
      name: "Installation method",
    });
    expect(selector).toHaveValue("");
    expect(within(selector).getAllByRole("option")).toHaveLength(8);
    expect(screen.queryByText(/dtxwiki update/)).not.toBeInTheDocument();
    expect(screen.queryByText(/I don.t know/i)).not.toBeInTheDocument();
  });

  it("hides the Deployment tab from non-admins", async () => {
    mockFetch("editor");
    render(
      <MemoryRouter initialEntries={["/settings?section=workspace"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    // Wait for the screen to settle on a known section control.
    expect(
      await screen.findByRole("button", { name: /MCP access/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Deployment/i })).not.toBeInTheDocument();
  });

  it("copies a command and reports Copied when the clipboard is available", async () => {
    const user = userEvent.setup();
    // Stub after userEvent.setup(): setup() installs its own clipboard stub.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    mockFetch("owner");
    render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await user.click(
      await screen.findByRole("button", { name: "Copy Change domain command" }),
    );

    expect(writeText).toHaveBeenCalledWith(
      "sudo dtxwiki config set DTXWIKI_PUBLIC_BASE_URL=https://new.example.com",
    );
    expect(
      await screen.findByRole("button", { name: "Copied" }),
    ).toBeInTheDocument();
  });

  it("falls back to selecting the command when the clipboard API is unavailable", async () => {
    const user = userEvent.setup();
    // The async clipboard API is missing in non-secure contexts (plain HTTP) -
    // the state the Deployment tab exists to fix.
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: false,
    });
    mockFetch("owner");
    render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    await user.click(
      await screen.findByRole("button", { name: "Copy Change domain command" }),
    );

    expect(
      await screen.findByRole("button", { name: "Selected - press Ctrl+C" }),
    ).toBeInTheDocument();
    expect(window.getSelection()?.rangeCount).toBe(1);
    expect(
      within(screen.getByText(/DTXWIKI_PUBLIC_BASE_URL/).closest(".deployment-command") as HTMLElement)
        .getByRole("button", { name: "Selected - press Ctrl+C" }),
    ).toBeInTheDocument();
  });

  it("does not render deployment content for non-admins deep-linking the section", async () => {
    // The nav entry is hidden, but a non-admin can still craft
    // ?section=deployment. The content render must be server-admin-gated too
    // (the API would 403 regardless) — otherwise a confusing empty pane appears.
    mockFetch("editor");
    render(
      <MemoryRouter initialEntries={["/settings?section=deployment"]}>
        <SettingsScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("button", { name: /MCP access/i }),
    ).toBeInTheDocument();
    // Instead of an empty pane, the deep link explains the server-admin gate.
    expect(
      await screen.findByText(/dtxWiki server \(system\) settings, visible to server admins only/i),
    ).toBeInTheDocument();
    // The owner-only deployment surface must NOT render via the deep link.
    expect(
      screen.queryByText(/run the shown command in a terminal/i),
    ).not.toBeInTheDocument();
  });
});
