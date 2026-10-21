import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createRef } from "react";

import type {
  McpAppCatalog,
  McpAuthMethod,
  McpConnectionOption,
  McpSetupPlan,
} from "../../../shared/api/mcpApps";
import { setLocale } from "../../../shared/i18n";
import {
  McpAppHub,
  SetupRecipeView,
  type McpAppHubHandle,
} from "../McpAppHub";

describe("McpAppHub", () => {
  beforeEach(() => setLocale("en", { persist: false }));

  it("announces catalog loading and retains the displayed apps while refreshing", () => {
    const props = {
      catalog: catalog(),
      error: null,
      loading: false,
      locale: "en" as const,
      loadPlan: async (owner: { id: string }, option: McpConnectionOption) =>
        plan(owner.id, option.auth, option.variant_id),
      onAccessKeySelected: vi.fn(),
      shell: "bash" as const,
    };
    const view = render(<McpAppHub {...props} catalog={null} loading />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Loading supported apps…");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status.querySelector(".spinner")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("button", { name: /Claude Code/ })).toBeNull();

    view.rerender(<McpAppHub {...props} />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("button", { name: /Claude Code/ })).toBeVisible();

    view.rerender(<McpAppHub {...props} loading />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading supported apps…");
    expect(screen.getByRole("button", { name: /Claude Code/ })).toBeVisible();

    view.rerender(
      <McpAppHub {...props} error="The catalog could not be refreshed." />,
    );
    expect(screen.queryByText("Loading supported apps…")).toBeNull();
    expect(screen.getByText("The catalog could not be refreshed.")).toBeVisible();
    expect(screen.getByRole("button", { name: /Claude Code/ })).toBeVisible();
  });

  it("discovers an ordinary synthetic app by vendor and alias without app-id code", async () => {
    const user = userEvent.setup();
    const onAccessKeySelected = vi.fn();
    const loadPlan = vi.fn(async (owner, option) =>
      plan(owner.id, option.auth, option.variant_id),
    );
    render(
      <McpAppHub
        catalog={catalog()}
        error={null}
        loading={false}
        locale="en"
        loadPlan={loadPlan}
        onAccessKeySelected={onAccessKeySelected}
        shell="bash"
      />,
    );

    const hub = document.querySelector("#mcp-app-hub") as HTMLElement;
    const featured = hub.querySelector(".mcp-featured-apps") as HTMLElement;
    expect(within(featured).getAllByRole("button")).toHaveLength(5);
    expect(
      within(featured).getAllByRole("button").at(-1),
    ).toHaveAccessibleName(/Browse all AI apps/);
    expect(
      within(hub).queryByRole("button", { name: "Connect AI app" }),
    ).toBeNull();
    expect(within(hub).queryByRole("button", { name: /Fifth App/ })).toBeNull();

    await user.click(
      within(hub).getByRole("button", { name: /Browse all AI apps/ }),
    );
    expect(screen.getByRole("button", { name: /Other MCP client/ })).toBeInTheDocument();
    const searchbox = screen.getByRole("searchbox", { name: "Search AI apps" });
    await user.type(searchbox, "does-not-exist");
    expect(screen.getByText(/No catalog app matches/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("0 catalog apps found");
    expect(screen.getByRole("button", { name: /Other MCP client/ })).toBeInTheDocument();
    await user.clear(searchbox);
    await user.type(searchbox, "nebula");
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("button", { name: /Nova Agent/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /Claude Code/ })).toBeNull();
    expect(
      within(dialog).getAllByRole("button", { name: /Nova Agent|Other MCP client/ }).at(-1),
    ).toHaveAccessibleName(expect.stringContaining("Other MCP client"));

    const novaTile = within(dialog).getByRole("button", { name: /Nova Agent/ });
    novaTile.focus();
    await user.keyboard("{Enter}");
    const methods = screen.getAllByRole("button", { name: /OAuth|Access Key/ });
    expect(methods[0]).toHaveTextContent("OAuth");
    expect(methods[0]).toBeDisabled();
    expect(methods[0]).toHaveAttribute(
      "data-reason-code",
      "server_oauth_unavailable",
    );
    expect(methods[0]).toHaveTextContent("OAuth is unavailable on this server.");
    await waitFor(() => expect(methods[1]).toHaveFocus());
    await user.click(screen.getByRole("button", { name: /Access Key/ }));

    await waitFor(() => expect(onAccessKeySelected).toHaveBeenCalledTimes(1));
    expect(onAccessKeySelected.mock.calls[0][0]).toMatchObject({
      owner: { id: "nova-agent", label: "Nova Agent" },
      option: { variant_id: "nova-agent-key" },
      plan: { owner_id: "nova-agent" },
      customAppLabel: null,
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("restores an unfocused mouse opener after Escape", async () => {
    render(
      <McpAppHub
        catalog={catalog()}
        error={null}
        loading={false}
        locale="en"
        loadPlan={async (owner, option) =>
          plan(owner.id, option.auth, option.variant_id)
        }
        onAccessKeySelected={vi.fn()}
        shell="bash"
      />,
    );

    const browse = screen.getByRole("button", { name: /Browse all AI apps/ });
    const otherButton = screen.getByRole("button", { name: /Claude Code/ });
    otherButton.focus();
    expect(browse).not.toHaveFocus();

    // fireEvent.click intentionally does not focus the button, matching
    // Safari's mouse-button behavior rather than userEvent's browser-neutral
    // focus simulation.
    fireEvent.click(browse);
    expect(screen.getByRole("dialog", { name: "Choose an AI app" })).toBeVisible();
    fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => expect(browse).toHaveFocus());
  });

  it("requires and preserves a user-entered label for a generic access-key app", async () => {
    const user = userEvent.setup();
    const onAccessKeySelected = vi.fn();
    const loadPlan = vi.fn(async (owner, option) =>
      plan(owner.id, option.auth, option.variant_id),
    );
    render(
      <McpAppHub
        catalog={catalog()}
        error={null}
        loading={false}
        locale="en"
        loadPlan={loadPlan}
        onAccessKeySelected={onAccessKeySelected}
        shell="bash"
      />,
    );

    await user.click(screen.getByRole("button", { name: /Browse all AI apps/ }));
    const search = screen.getByRole("searchbox", { name: "Search AI apps" });
    await user.type(search, "  Grok Desktop  ");
    await user.click(screen.getByRole("button", { name: /Other MCP client/ }));

    const customLabel = screen.getByRole("textbox", {
      name: "AI app display name",
    });
    expect(customLabel).toHaveValue("Grok Desktop");
    await user.clear(customLabel);
    await user.click(screen.getByRole("button", { name: /Access Key/ }));
    expect(await screen.findByText(/Enter the AI app name/)).toBeInTheDocument();
    expect(customLabel).toHaveFocus();
    expect(loadPlan).not.toHaveBeenCalled();

    await user.type(customLabel, "Grok Workbench");
    await user.click(screen.getByRole("button", { name: /Access Key/ }));
    await waitFor(() => expect(onAccessKeySelected).toHaveBeenCalledOnce());
    expect(onAccessKeySelected.mock.calls[0][0]).toMatchObject({
      owner: { kind: "generic-profile", id: "other-mcp-client" },
      customAppLabel: "Grok Workbench",
    });
  });

  it("loads an OAuth-first setup recipe as copy-only text", async () => {
    const user = userEvent.setup();
    const current = catalog();
    const nova = current.apps.find((app) => app.id === "nova-agent");
    if (!nova) throw new Error("missing synthetic app");
    nova.options[0] = { ...nova.options[0], available: true, reason_codes: [] };
    const loadPlan = vi.fn(async (owner, option) =>
      plan(owner.id, option.auth, option.variant_id),
    );
    render(
      <McpAppHub
        catalog={current}
        error={null}
        loading={false}
        locale="en"
        loadPlan={loadPlan}
        onAccessKeySelected={vi.fn()}
        shell="bash"
      />,
    );

    await user.click(screen.getByRole("button", { name: /Browse all AI apps/ }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: /Nova Agent/,
      }),
    );
    const methodButtons = screen.getAllByRole("button", {
      name: /OAuth|Access Key/,
    });
    expect(methodButtons[0]).toHaveTextContent("OAuth");
    await waitFor(() => expect(methodButtons[0]).toHaveFocus());
    await user.click(methodButtons[0]);

    expect(await screen.findByText("Setup steps")).toBeInTheDocument();
    await waitFor(() =>
      expect(document.querySelector(".mcp-app-recipe-focus-target"))
        .toHaveFocus(),
    );
    expect(screen.getByText("Bash")).toHaveClass("mcp-shell-badge");
    expect(screen.getByLabelText("Register command · Bash")).toHaveAttribute(
      "tabindex",
      "0",
    );
    expect(
      screen.getByRole("button", { name: "Copy Register command for Bash" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/nova mcp add https:\/\/wiki\.example\/mcp/)).toBeInTheDocument();
    expect(screen.getByText("Run one read to verify.")).toBeInTheDocument();
    expect(loadPlan).toHaveBeenCalledWith(
      expect.objectContaining({ id: "nova-agent" }),
      expect.objectContaining({ auth: "oauth" }),
    );
  });

  it("keeps a disabled catalog app out of Quick connect", async () => {
    const current = catalog();
    current.apps[0] = { ...current.apps[0], lifecycle: "disabled" };
    const user = userEvent.setup();
    render(
      <McpAppHub
        catalog={current}
        error={null}
        loading={false}
        locale="en"
        loadPlan={async (owner, option) =>
          plan(owner.id, option.auth, option.variant_id)
        }
        onAccessKeySelected={vi.fn()}
        shell="bash"
      />,
    );

    const quickConnect = screen.getByLabelText("Quick connect");
    expect(within(quickConnect).queryByRole("button", { name: /Claude Code/ }))
      .toBeNull();
    await user.click(screen.getByRole("button", { name: /Browse all AI apps/ }));
    expect(screen.getByRole("button", { name: /Claude Code/ }))
      .toBeInTheDocument();
  });

  it("keeps Browse all available when the catalog has no featured apps", () => {
    const current = catalog();
    current.apps = current.apps.map((entry) => ({
      ...entry,
      lifecycle: "disabled" as const,
    }));
    render(
      <McpAppHub
        catalog={current}
        error={null}
        loading={false}
        locale="en"
        loadPlan={async (owner, selectedOption) =>
          plan(owner.id, selectedOption.auth, selectedOption.variant_id)
        }
        onAccessKeySelected={vi.fn()}
        shell="bash"
      />,
    );

    expect(screen.getByText(/No featured apps are available/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Browse all AI apps/ }),
    ).toBeEnabled();
  });

  it("offers an explicit retry when the catalog cannot be loaded", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(
      <McpAppHub
        catalog={null}
        error="The AI app catalog could not be loaded."
        loading={false}
        locale="en"
        loadPlan={async (owner, selectedOption) =>
          plan(owner.id, selectedOption.auth, selectedOption.variant_id)
        }
        onAccessKeySelected={vi.fn()}
        onRetry={onRetry}
        shell="bash"
      />,
    );

    await user.click(screen.getByRole("button", { name: "Retry app catalog" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("localizes known and unknown unavailable reasons without exposing raw codes", async () => {
    await setLocale("ko", { persist: false });
    const current = catalog();
    const nova = current.apps.find((app) => app.id === "nova-agent");
    if (!nova) throw new Error("missing synthetic app");
    nova.options[1] = {
      ...nova.options[1],
      available: false,
      reason_codes: ["unknown"],
    };
    const user = userEvent.setup();
    render(
      <McpAppHub
        catalog={current}
        error={null}
        loading={false}
        locale="ko"
        loadPlan={async (owner, option) =>
          plan(owner.id, option.auth, option.variant_id)
        }
        onAccessKeySelected={vi.fn()}
        shell="bash"
      />,
    );

    await user.click(screen.getByRole("button", { name: /모든 AI 앱 보기/ }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: /Nova Agent/,
      }),
    );

    expect(screen.getByText("이 서버에서는 OAuth를 사용할 수 없습니다."))
      .toBeInTheDocument();
    expect(screen.getByText("현재 설정에서는 이 연결 방식을 사용할 수 없습니다."))
      .toBeInTheDocument();
    expect(screen.queryByText(/future catalog reason/i)).toBeNull();
  });

  it.each([
    [
      "en",
      [
        "This catalog app is unavailable.",
        "This MCP transport is not supported.",
        "OAuth is unavailable on this server.",
        "The server URL is not reachable by this method.",
        "Choose a platform to check this method.",
        "This platform is not supported.",
        "Choose a shell to check this method.",
        "This shell is not supported.",
        "Enter the app version to check this method.",
        "Update the app to use this method.",
      ],
    ],
    [
      "ko",
      [
        "이 카탈로그 앱은 사용할 수 없습니다.",
        "이 MCP 전송 방식은 지원되지 않습니다.",
        "이 서버에서는 OAuth를 사용할 수 없습니다.",
        "이 연결 방식으로는 서버 URL에 접근할 수 없습니다.",
        "이 연결 방식을 확인하려면 플랫폼을 선택하세요.",
        "이 플랫폼은 지원되지 않습니다.",
        "이 연결 방식을 확인하려면 셸을 선택하세요.",
        "이 셸은 지원되지 않습니다.",
        "이 연결 방식을 확인하려면 앱 버전을 입력하세요.",
        "이 연결 방식을 사용하려면 앱을 업데이트하세요.",
      ],
    ],
  ] as const)(
    "renders every canonical backend reason in %s",
    async (locale, labels) => {
      await setLocale(locale, { persist: false });
      const current = catalog();
      const nova = current.apps.find((app) => app.id === "nova-agent");
      if (!nova) throw new Error("missing synthetic app");
      nova.options[0] = {
        ...nova.options[0],
        available: false,
        reason_codes: [
          "catalog_app_unavailable",
          "transport_unavailable",
          "server_oauth_unavailable",
          "endpoint_unreachable",
          "platform_required",
          "platform_unsupported",
          "shell_required",
          "shell_unsupported",
          "app_version_required",
          "app_version_too_old",
        ],
      };
      const user = userEvent.setup();
      render(
        <McpAppHub
          catalog={current}
          error={null}
          loading={false}
          locale={locale}
          loadPlan={async (owner, option) =>
            plan(owner.id, option.auth, option.variant_id)
          }
          onAccessKeySelected={vi.fn()}
          shell="bash"
        />,
      );

      await user.click(
        screen.getByRole("button", {
          name: locale === "ko" ? /모든 AI 앱 보기/ : /Browse all AI apps/,
        }),
      );
      await user.click(
        within(screen.getByRole("dialog")).getByRole("button", {
          name: /Nova Agent/,
        }),
      );
      const reasons = document.querySelector(".mcp-method-reasons");
      expect(reasons).toBeInTheDocument();
      for (const label of labels) {
        expect(reasons).toHaveTextContent(label);
      }
    },
  );

  it("shows a localized fail-closed message when the setup plan is rejected", async () => {
    await setLocale("ko", { persist: false });
    const current = catalog();
    const nova = current.apps.find((app) => app.id === "nova-agent");
    if (!nova) throw new Error("missing synthetic app");
    nova.options[0] = { ...nova.options[0], available: true, reason_codes: [] };
    const user = userEvent.setup();
    render(
      <McpAppHub
        catalog={current}
        error={null}
        loading={false}
        locale="ko"
        loadPlan={async () => {
          throw new Error("Unsupported MCP setup recipe version: 2");
        }}
        onAccessKeySelected={vi.fn()}
        shell="bash"
      />,
    );

    await user.click(screen.getByRole("button", { name: /모든 AI 앱 보기/ }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: /Nova Agent/,
      }),
    );
    await user.click(screen.getByRole("button", { name: /^OAuth/ }));

    expect(await screen.findByText("현재 설정을 생성할 수 없습니다."))
      .toBeInTheDocument();
    expect(screen.queryByText(/Unsupported MCP setup recipe version/)).toBeNull();
  });

  it("ignores an older owner error and keeps the newer loading status announced", async () => {
    const user = userEvent.setup();
    const claudeRequest = deferred<McpSetupPlan>();
    const codexRequest = deferred<McpSetupPlan>();
    const loadPlan = vi.fn((owner) =>
      owner.id === "claude-code"
        ? claudeRequest.promise
        : codexRequest.promise,
    );
    render(
      <McpAppHub
        catalog={catalog()}
        error={null}
        loading={false}
        locale="en"
        loadPlan={loadPlan}
        onAccessKeySelected={vi.fn()}
        shell="bash"
      />,
    );

    await user.click(screen.getByRole("button", { name: /Browse all AI apps/ }));
    let dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /Claude Code/ }));
    await user.click(within(dialog).getByRole("button", { name: /^OAuth/ }));
    await user.click(within(dialog).getByRole("button", { name: "All apps" }));
    dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /Codex CLI/ }));
    const codexOAuth = within(dialog).getByRole("button", { name: /^OAuth/ });
    await user.click(codexOAuth);
    const preparationStatus = within(dialog).getByRole("status");
    expect(preparationStatus).toHaveTextContent("Preparing setup…");
    expect(preparationStatus).toHaveAttribute("aria-live", "polite");
    expect(preparationStatus.closest('[aria-busy="true"]')).toBeNull();
    expect(preparationStatus.querySelector(".spinner"))
      .toHaveAttribute("aria-hidden", "true");

    await act(async () => {
      claudeRequest.reject(new Error("late Claude failure"));
      await claudeRequest.promise.catch(() => undefined);
    });
    expect(screen.queryByText("The current setup could not be generated."))
      .not.toBeInTheDocument();
    expect(within(dialog).getByRole("status")).toBe(preparationStatus);
    expect(codexOAuth).toBeDisabled();
    expect(screen.getByRole("heading", { name: "Codex CLI" }))
      .toBeInTheDocument();

    await act(async () => {
      codexRequest.resolve(plan("codex", "oauth", "codex-oauth"));
      await codexRequest.promise;
    });
    expect(await screen.findByText("Setup steps")).toBeInTheDocument();
    expect(screen.queryByText("Preparing setup…")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Codex CLI" }))
      .toBeInTheDocument();
  });

  it("does not apply an access-key plan after the picker is closed", async () => {
    const user = userEvent.setup();
    const request = deferred<McpSetupPlan>();
    const onAccessKeySelected = vi.fn();
    render(
      <McpAppHub
        catalog={catalog()}
        error={null}
        loading={false}
        locale="en"
        loadPlan={() => request.promise}
        onAccessKeySelected={onAccessKeySelected}
        shell="bash"
      />,
    );

    await user.click(screen.getByRole("button", { name: /Browse all AI apps/ }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /Claude Code/ }));
    await user.click(within(dialog).getByRole("button", { name: /Access Key/ }));
    await user.click(
      within(dialog).getByRole("button", { name: "Close app picker" }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await act(async () => {
      request.resolve(plan("claude-code", "access-key", "claude-code-key"));
      await request.promise;
    });
    expect(onAccessKeySelected).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("rejects a type-correct setup plan projected for another owner", async () => {
    const user = userEvent.setup();
    const current = catalog();
    const nova = current.apps.find((app) => app.id === "nova-agent");
    if (!nova) throw new Error("missing synthetic app");
    nova.options[0] = { ...nova.options[0], available: true, reason_codes: [] };
    render(
      <McpAppHub
        catalog={current}
        error={null}
        loading={false}
        locale="en"
        loadPlan={async (_owner, option) =>
          plan("codex", option.auth, option.variant_id)
        }
        onAccessKeySelected={vi.fn()}
        shell="bash"
      />,
    );

    await user.click(screen.getByRole("button", { name: /Browse all AI apps/ }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /Nova Agent/ }));
    await user.click(within(dialog).getByRole("button", { name: /^OAuth/ }));

    expect(await screen.findByText("The current setup could not be generated."))
      .toBeInTheDocument();
    expect(screen.queryByText("Setup steps")).toBeNull();
  });

  it("commits the access-key method immediately when opened from Access key setup", async () => {
    const user = userEvent.setup();
    const ref = createRef<McpAppHubHandle>();
    const onAccessKeySelected = vi.fn();
    render(
      <McpAppHub
        catalog={catalog()}
        error={null}
        loading={false}
        locale="en"
        loadPlan={async (owner, option) =>
          plan(owner.id, option.auth, option.variant_id)
        }
        onAccessKeySelected={onAccessKeySelected}
        ref={ref}
        shell="bash"
      />,
    );

    act(() =>
      ref.current?.open({
        behavior: "commit-method",
        method: "access-key",
        source: "access-key-setup",
      }),
    );
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByText("Connection method")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: /Nova Agent/ }));

    await waitFor(() => expect(onAccessKeySelected).toHaveBeenCalledOnce());
    expect(onAccessKeySelected.mock.calls[0][0]).toMatchObject({
      owner: { id: "nova-agent" },
      option: { auth: "access-key" },
      source: "access-key-setup",
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("disables apps that cannot use the method required by the invoking setup", () => {
    const ref = createRef<McpAppHubHandle>();
    const current = catalog();
    const nova = current.apps.find((app) => app.id === "nova-agent");
    if (!nova) throw new Error("missing synthetic app");
    nova.options[1] = {
      ...nova.options[1],
      available: false,
      reason_codes: ["shell_unsupported"],
    };
    render(
      <McpAppHub
        catalog={current}
        error={null}
        loading={false}
        locale="en"
        loadPlan={async (owner, option) =>
          plan(owner.id, option.auth, option.variant_id)
        }
        onAccessKeySelected={vi.fn()}
        ref={ref}
        shell="bash"
      />,
    );

    act(() =>
      ref.current?.open({
        behavior: "commit-method",
        method: "access-key",
        source: "access-key-setup",
      }),
    );
    const novaTile = within(screen.getByRole("dialog")).getByRole("button", {
      name: /Nova Agent/,
    });
    expect(novaTile).toBeDisabled();
    expect(novaTile).toHaveTextContent("This shell is not supported.");
  });

  it("fences an in-flight setup plan when the workspace context changes", async () => {
    const user = userEvent.setup();
    const ref = createRef<McpAppHubHandle>();
    const request = deferred<McpSetupPlan>();
    const onAccessKeySelected = vi.fn();
    const props = {
      catalog: catalog(),
      error: null,
      loading: false,
      locale: "en" as const,
      loadPlan: () => request.promise,
      onAccessKeySelected,
      ref,
      shell: "bash" as const,
    };
    const view = render(<McpAppHub {...props} contextKey="workspace-a" />);

    act(() =>
      ref.current?.open({
        behavior: "commit-method",
        method: "access-key",
        source: "access-key-setup",
      }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: /Claude Code/,
      }),
    );
    view.rerender(<McpAppHub {...props} contextKey="workspace-b" />);
    expect(screen.queryByRole("dialog")).toBeNull();

    await act(async () => {
      request.resolve(plan("claude-code", "access-key", "claude-code-key"));
      await request.promise;
    });
    expect(onAccessKeySelected).not.toHaveBeenCalled();
  });

  it("fences an in-flight setup plan when the picker unmounts", async () => {
    const user = userEvent.setup();
    const ref = createRef<McpAppHubHandle>();
    const request = deferred<McpSetupPlan>();
    const onAccessKeySelected = vi.fn();
    const view = render(
      <McpAppHub
        catalog={catalog()}
        error={null}
        loading={false}
        locale="en"
        loadPlan={() => request.promise}
        onAccessKeySelected={onAccessKeySelected}
        ref={ref}
        shell="bash"
      />,
    );

    act(() =>
      ref.current?.open({
        behavior: "commit-method",
        method: "access-key",
        source: "access-key-setup",
      }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: /Claude Code/,
      }),
    );
    view.unmount();

    await act(async () => {
      request.resolve(plan("claude-code", "access-key", "claude-code-key"));
      await request.promise;
    });
    expect(onAccessKeySelected).not.toHaveBeenCalled();
  });

  it("uses an available same-auth variant when a higher-priority variant is unavailable", async () => {
    const user = userEvent.setup();
    const ref = createRef<McpAppHubHandle>();
    const current = catalog();
    const nova = current.apps.find((app) => app.id === "nova-agent");
    if (!nova) throw new Error("missing synthetic app");
    nova.options[1] = {
      ...nova.options[1],
      available: false,
      reason_codes: ["shell_unsupported"],
    };
    nova.options.push({
      ...option("nova-agent-key-compatible", "access-key"),
      priority: 30,
    });
    const loadPlan = vi.fn(async (owner, selectedOption) =>
      plan(owner.id, selectedOption.auth, selectedOption.variant_id),
    );
    const onAccessKeySelected = vi.fn();
    render(
      <McpAppHub
        catalog={current}
        error={null}
        loading={false}
        locale="en"
        loadPlan={loadPlan}
        onAccessKeySelected={onAccessKeySelected}
        ref={ref}
        shell="bash"
      />,
    );

    act(() =>
      ref.current?.open({
        behavior: "commit-method",
        method: "access-key",
        source: "access-key-setup",
      }),
    );
    const novaTile = within(screen.getByRole("dialog")).getByRole("button", {
      name: /Nova Agent/,
    });
    expect(novaTile).toBeEnabled();
    await user.click(novaTile);

    await waitFor(() => expect(onAccessKeySelected).toHaveBeenCalledOnce());
    expect(loadPlan).toHaveBeenCalledWith(
      expect.objectContaining({ id: "nova-agent" }),
      expect.objectContaining({ variant_id: "nova-agent-key-compatible" }),
    );
  });

  it("falls back to selecting command text when clipboard writing is rejected", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockRejectedValue(new Error("permission denied"));
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <SetupRecipeView
        locale="en"
        plan={plan("nova-agent", "oauth", "nova-agent-oauth")}
        shell="bash"
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Copy Register command for Bash" }),
    );
    expect(writeText).toHaveBeenCalledOnce();
    expect(await screen.findByText("Selected")).toBeInTheDocument();
    expect(screen.queryByText("Copied")).toBeNull();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
  });

  it("shows the OAuth sign-in step with its own command and app badge", () => {
    const oauthPlan: McpSetupPlan = {
      ...plan("gemini-cli", "oauth", "gemini-cli-oauth"),
      artifacts: [
        {
          kind: "command",
          step: "register",
          executableId: "gemini-executable",
          commandName: "gemini",
          argv: [[{ literal: "mcp" }], [{ literal: "add" }], [{ slot: "mcp-url" }]],
        },
        {
          kind: "app-command",
          step: "authenticate",
          value: [{ literal: "/mcp auth " }, { slot: "server-name" }],
        },
      ],
    };
    render(
      <SetupRecipeView
        appLabel="Gemini CLI"
        locale="en"
        plan={oauthPlan}
        shell="bash"
      />,
    );

    const steps = screen.getAllByRole("listitem");
    expect(steps).toHaveLength(2);
    expect(steps[0]).toHaveTextContent("Step 1 of 2");
    expect(within(steps[0]).getByText("Register the server")).toBeInTheDocument();
    expect(steps[1]).toHaveTextContent("Step 2 of 2");
    expect(within(steps[1]).getByText("Sign in with OAuth")).toBeInTheDocument();
    // The login command is the thing users came for: it is a labelled,
    // copyable block, not a sentence buried under the recipe.
    expect(within(steps[1]).getByText("In-app login command")).toBeInTheDocument();
    expect(within(steps[1]).getByText("/mcp auth dtxwiki")).toBeInTheDocument();
    // It is typed into Gemini CLI, so it must not wear the shell badge.
    expect(within(steps[1]).getByText("Type in Gemini CLI")).toBeInTheDocument();
    expect(within(steps[1]).queryByText("Bash")).toBeNull();
    expect(
      // Copy vs Select depends on clipboard availability; both accessible
      // names must name the app, never the shell.
      screen.getByRole("button", {
        name: /In-app login command to type in Gemini CLI/,
      }),
    ).toBeInTheDocument();
  });

  it("closes on Escape and restores focus to the common picker trigger", async () => {
    const user = userEvent.setup();
    render(
      <McpAppHub
        catalog={catalog()}
        error={null}
        loading={false}
        locale="en"
        loadPlan={async (owner, option) =>
          plan(owner.id, option.auth, option.variant_id)
        }
        onAccessKeySelected={vi.fn()}
        shell="bash"
      />,
    );
    const trigger = screen.getByRole("button", { name: /Browse all AI apps/ });
    trigger.focus();
    await user.click(trigger);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
  });
});

function catalog(): McpAppCatalog {
  const apps = [
    app("claude-code", "Claude Code", "Anthropic", 1),
    app("codex", "Codex CLI", "OpenAI", 2),
    app("nova-agent", "Nova Agent", "Example Labs", 3, ["nebula", "nova lab"]),
    app("fourth-app", "Fourth App", "Example", 4),
    app("fifth-app", "Fifth App", "Example", 5),
  ];
  apps[2].options[0] = {
    ...apps[2].options[0],
    available: false,
    reason_codes: ["server_oauth_unavailable"],
  };
  return {
    schema_major: 1,
    catalog_revision: "test.1",
    apps,
    generic_profiles: [
      {
        id: "other-mcp-client",
        label: "Other MCP client",
        icon_url: "/generic.svg",
        options: [option("other-oauth", "oauth"), option("other-key", "access-key")],
      },
    ],
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

function app(
  id: string,
  label: string,
  vendor: string,
  rank: number,
  aliases: string[] = [],
) {
  return {
    id,
    label,
    aliases,
    icon_url: `/${id}.svg`,
    vendor,
    featured_rank: rank,
    lifecycle: "active" as const,
    verification_tier: "community" as const,
    options: [
      option(`${id}-oauth`, "oauth"),
      option(`${id}-key`, "access-key"),
    ],
  };
}

function option(id: string, auth: McpAuthMethod) {
  return {
    variant_id: id,
    auth,
    transport: "streamable-http" as const,
    surface: "cli" as const,
    default_scope_preset: "agent-draft" as const,
    available: true,
    reason_codes: [] as McpConnectionOption["reason_codes"],
    priority: auth === "oauth" ? 10 : 20,
  };
}

function plan(
  ownerId: string,
  auth: McpAuthMethod,
  variantId: string,
): McpSetupPlan {
  return {
    catalog_revision: "test.1",
    owner_kind: ownerId === "other-mcp-client" ? "generic-profile" : "app",
    owner_id: ownerId,
    variant_id: variantId,
    recipe_id: `${variantId}-r1`,
    recipe_version: 1,
    auth,
    transport: "streamable-http",
    surface: "cli",
    default_scope_preset: "agent-draft",
    mcp_url: "https://wiki.example/mcp",
    artifacts: [
      {
        kind: "command",
        step: "register",
        executableId: "nova-executable",
        commandName: "nova",
        argv: [[{ literal: "mcp" }], [{ literal: "add" }], [{ slot: "mcp-url" }]],
      },
    ],
    verification_instruction: "Run one read to verify.",
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}
