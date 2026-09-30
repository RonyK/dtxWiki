import { afterEach, describe, expect, it, vi } from "vitest";

import { createMcpAppSetupPlan, getMcpAppCatalog } from "../mcpApps";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

describe("MCP app catalog API adapter", () => {
  afterEach(() => vi.restoreAllMocks());

  it("isolates and validates catalog and setup-plan transport DTOs", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ data: catalogData() }))
      .mockResolvedValueOnce(
        jsonResponse({
          data: setupPlan({
            kind: "command",
            step: "register",
            executable_id: "nova-executable",
            command_name: "nova",
            argv: [[{ literal: "mcp" }], [{ slot: "mcp-url" }]],
            env: { DTXWIKI_MCP_PAT: [{ slot: "access-token-env" }] },
          }),
        }),
      );

    await expect(
      getMcpAppCatalog(WORKSPACE_ID, {
        locale: "ko",
        platform: "windows",
        shell: "powershell",
      }),
    ).resolves.toMatchObject({
      catalog_revision: "test.1",
      apps: [{ id: "nova", label: "Nova" }],
    });
    await expect(
      createMcpAppSetupPlan(
        WORKSPACE_ID,
        { kind: "app", id: "nova" },
        {
          variant_id: "nova-key",
          locale: "ko",
          platform: "windows",
          shell: "powershell",
        },
      ),
    ).resolves.toMatchObject({
      owner_id: "nova",
      artifacts: [{ commandName: "nova", executableId: "nova-executable" }],
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps?locale=ko&platform=windows&shell=powershell`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-apps/nova/setup-plan`,
      expect.objectContaining({
        body: JSON.stringify({
          variant_id: "nova-key",
          locale: "ko",
          platform: "windows",
          shell: "powershell",
        }),
        credentials: "include",
        method: "POST",
      }),
    );
  });

  it("normalizes an unknown future compatibility reason at the transport boundary", async () => {
    const catalog = catalogData();
    const optionRecord = catalog.apps[0].options[0] as unknown as Record<
      string,
      unknown
    >;
    optionRecord.reason_codes = ["future_catalog_reason"];
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: catalog }),
    );

    const result = await getMcpAppCatalog(WORKSPACE_ID);

    expect(result.apps[0].options[0].reason_codes).toEqual(["unknown"]);
  });

  it("fails closed for an unknown setup artifact", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: setupPlan({ kind: "run-script", script: "oops" }) }),
    );

    await expect(
      createMcpAppSetupPlan(
        WORKSPACE_ID,
        { kind: "app", id: "nova" },
        { variant_id: "nova-key" },
      ),
    ).rejects.toThrow("Unsupported MCP setup artifact");
  });

  it.each([
    {
      name: "an access key in an arbitrary argv position",
      artifact: commandArtifact({ argv: [[{ slot: "access-token" }]] }),
    },
    {
      name: "the wrong secret atom in an environment value",
      artifact: commandArtifact({
        env: { DTXWIKI_MCP_PAT: [{ slot: "access-token" }] },
      }),
    },
    {
      name: "an unsafe environment name",
      artifact: commandArtifact({
        env: { "unsafe-name": [{ slot: "access-token-env" }] },
      }),
    },
  ])("rejects $name without an app-id branch", async ({ artifact }) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: setupPlan(artifact) }),
    );

    await expect(
      createMcpAppSetupPlan(
        WORKSPACE_ID,
        { kind: "app", id: "nova" },
        { variant_id: "nova-key" },
      ),
    ).rejects.toThrow("unapproved command sink");
  });

  it.each(["bash", "PowerShell.exe"])(
    "rejects the shell interpreter %s as an executable",
    async (commandName) => {
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse({
          data: setupPlan(commandArtifact({ commandName })),
        }),
      );

      await expect(
        createMcpAppSetupPlan(
          WORKSPACE_ID,
          { kind: "app", id: "nova" },
          { variant_id: "nova-key" },
        ),
      ).rejects.toThrow("Shell interpreter is not an approved");
    },
  );

  it.each(["-c", "/c", "-Command", "--command"])(
    "rejects command-string argument %s",
    async (argument) => {
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse({
          data: setupPlan(
            commandArtifact({ argv: [[{ literal: argument }]] }),
          ),
        }),
      );

      await expect(
        createMcpAppSetupPlan(
          WORKSPACE_ID,
          { kind: "app", id: "nova" },
          { variant_id: "nova-key" },
        ),
      ).rejects.toThrow("command-string mode is not allowed");
    },
  );

  it.each([{ argv: [] }, { argv: [[]] }])(
    "rejects empty command arguments %#",
    async ({ argv }) => {
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse({
          data: setupPlan(commandArtifact({ argv })),
        }),
      );

      await expect(
        createMcpAppSetupPlan(
          WORKSPACE_ID,
          { kind: "app", id: "nova" },
          { variant_id: "nova-key" },
        ),
      ).rejects.toThrow("Invalid MCP setup command arguments");
    },
  );

  it("rejects an access-key plan with no approved credential sink", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: setupPlan(commandArtifact({ env: null })),
      }),
    );

    await expect(
      createMcpAppSetupPlan(
        WORKSPACE_ID,
        { kind: "app", id: "nova" },
        { variant_id: "nova-key" },
      ),
    ).rejects.toThrow("must include an approved credential sink");
  });

  it("rejects an OAuth plan containing an access-key credential sink", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          ...setupPlan(commandArtifact()),
          auth: "oauth",
        },
      }),
    );

    await expect(
      createMcpAppSetupPlan(
        WORKSPACE_ID,
        { kind: "app", id: "nova" },
        { variant_id: "nova-oauth" },
      ),
    ).rejects.toThrow("must not include a credential sink");
  });

  it.each([
    {
      name: "a secret in a URL field",
      value: {
        url: { slot: "access-token" },
        bearerToken: { slot: "access-token" },
      },
      message: "unapproved JSON sink",
    },
    {
      name: "a nested bearerToken field",
      value: { nested: { bearerToken: "not-even-a-secret" } },
      message: "Nested bearerToken",
    },
    {
      name: "an environment secret slot",
      value: { bearerToken: { slot: "access-token-env" } },
      message: "unapproved JSON sink",
    },
    {
      name: "an unknown slot",
      value: { url: { slot: "future-secret-slot" } },
      message: "Unknown MCP JSON setup slot",
    },
    {
      name: "an atom with extra properties",
      value: {
        bearerToken: { slot: "access-token", literal: "smuggled" },
      },
      message: "extra properties",
    },
  ])("rejects $name in a closed JSON template", async ({ message, value }) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: setupPlan({
          kind: "json-config",
          step: "register",
          label: { en: "Config", ko: "설정" },
          value,
        }),
      }),
    );

    await expect(
      createMcpAppSetupPlan(
        WORKSPACE_ID,
        { kind: "generic-profile", id: "other-mcp-client" },
        { variant_id: "other-key" },
      ),
    ).rejects.toThrow(message);
  });

  it("rejects recipe versions the renderer does not understand", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          ...setupPlan(commandArtifact()),
          recipe_version: 2,
        },
      }),
    );

    await expect(
      createMcpAppSetupPlan(
        WORKSPACE_ID,
        { kind: "app", id: "nova" },
        { variant_id: "nova-key" },
      ),
    ).rejects.toThrow("Unsupported MCP setup recipe version: 2");
  });

  it.each([
    ["available", "yes", "Invalid option availability"],
    ["priority", "first", "Invalid option priority"],
  ])("rejects option %s type drift instead of coercing it", async (field, value, message) => {
    const catalog = catalogData();
    const optionRecord = catalog.apps[0].options[0] as unknown as Record<
      string,
      unknown
    >;
    optionRecord[field] = value;
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: catalog }),
    );

    await expect(getMcpAppCatalog(WORKSPACE_ID)).rejects.toThrow(message);
  });
});

function commandArtifact(overrides: Record<string, unknown> = {}) {
  return {
    kind: "command",
    step: "register",
    executableId: "nova-executable",
    commandName: "nova",
    argv: [[{ literal: "mcp" }]],
    env: { DTXWIKI_MCP_PAT: [{ slot: "access-token-env" }] },
    ...overrides,
  };
}

function catalogData() {
  return {
    schema_major: 1,
    catalog_revision: "test.1",
    apps: [
      {
        id: "nova",
        label: "Nova",
        aliases: ["nova lab"],
        icon_url: "/nova.svg",
        vendor: "Example Labs",
        featured_rank: 1,
        lifecycle: "active",
        verification_tier: "community",
        options: [option()],
      },
    ],
    generic_profiles: [],
    server_capabilities: {
      oauth_available: true,
      oauth_unavailable_reason: null,
      oauth_dcr_enabled: true,
      mcp_url: "https://wiki.example.com/mcp",
      transports: ["streamable-http"],
      endpoint_reachability: ["public-https"],
    },
  };
}

function option() {
  return {
    variant_id: "nova-key",
    auth: "access-key",
    transport: "streamable-http",
    surface: "cli",
    default_scope_preset: "agent-draft",
    available: true,
    reason_codes: [],
    priority: 20,
  };
}

function setupPlan(artifact: unknown) {
  return {
    catalog_revision: "test.1",
    owner_kind: "app",
    owner_id: "nova",
    variant_id: "nova-key",
    recipe_id: "nova-key-r1",
    recipe_version: 1,
    auth: "access-key",
    transport: "streamable-http",
    surface: "cli",
    default_scope_preset: "agent-draft",
    mcp_url: "https://wiki.example/mcp",
    artifacts: [artifact],
    verification_instruction: "Run one read.",
  };
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
