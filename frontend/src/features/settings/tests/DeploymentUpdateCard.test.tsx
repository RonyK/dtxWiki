import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import type { DeploymentUpdateStatus } from "../../../shared/api/operations";
import { DeploymentUpdateCard } from "../DeploymentUpdateCard";

const FRESH: DeploymentUpdateStatus = {
  current_version: "0.1.0",
  latest_version: "0.2.0",
  update_available: true,
  check_status: "succeeded",
  checked_at: "2026-10-23T00:00:00Z",
  stale: false,
  source: "pypi",
};

function response(data: DeploymentUpdateStatus) {
  return new Response(JSON.stringify({ data }), {
    headers: { "Content-Type": "application/json" },
  });
}

function card(initialStatus = FRESH) {
  return <DeploymentUpdateCard initialStatus={initialStatus} renderCommand={(command) => <pre>{command}</pre>} />;
}

describe("Deployment update guidance", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setLocale("en", { persist: false });
  });

  it("switches all installation guides locally and separates npx launch from verification", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    render(card());
    const user = userEvent.setup();
    const selector = screen.getByRole("combobox");
    const commands: Record<string, string> = {
      uv: 'uv tool install --force "dtxwiki==0.2.0"',
      pipx: 'pipx install --force "dtxwiki==0.2.0"',
      pip: 'python -m pip install --upgrade "dtxwiki==0.2.0"',
      npx: "npx @dtxsuite/dtxwiki@latest",
      npm: "npm install --global @dtxsuite/dtxwiki@latest",
    };
    for (const [method, command] of Object.entries(commands)) {
      await user.selectOptions(selector, method);
      expect(screen.getByText(command)).toBeInTheDocument();
      expect(screen.getByRole("list").children).toHaveLength(3);
      if (method === "uv") {
        expect(screen.getByText(/Requires Python 3.12 or newer/)).toHaveTextContent("--python <version>");
        expect(screen.queryByText(/--python/, { selector: "pre" })).not.toBeInTheDocument();
      }
      if (method === "npx") {
        const preparation = screen.getByRole("list").children[0] as HTMLElement;
        expect(within(preparation).getByText(/Before starting, run the release's required checks/)).toHaveTextContent("npx @dtxsuite/dtxwiki@<version> inputs audit");
        expect(screen.getByText(/Replace @latest below with the same npm version/)).toBeInTheDocument();
        expect(screen.getByRole("link", { name: "Detailed npx (run directly) instructions" })).toHaveAttribute("href", expect.stringContaining("#npx"));
        expect(screen.getByRole("heading", { name: "Start the latest version" })).toBeInTheDocument();
        expect(screen.getByRole("heading", { name: "Check the version" })).toBeInTheDocument();
        expect(screen.getByText(/do not launch a second server/)).toBeInTheDocument();
      }
    }
    for (const method of ["docker", "service"]) {
      await user.selectOptions(selector, method);
      expect(screen.queryByText(/^(?:uv |pipx |python |npx |npm )/, { selector: "pre" })).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Detailed .* instructions/ })).toHaveAttribute("href", expect.stringContaining(method === "docker" ? "#docker-rebuild-from-a-pinned-version" : "#systemd-a-verified-local-wheel"));
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it("checks an absent cache once, forces manual checks, and withdraws a Python target on failure", async () => {
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(FRESH))
      .mockResolvedValueOnce(response({ ...FRESH, check_status: "failed" }))
      .mockResolvedValueOnce(response(FRESH));
    render(card({ ...FRESH, latest_version: null, update_available: false, check_status: "not_checked", checked_at: null }));
    expect(await screen.findByText("New release")).toBeInTheDocument();
    expect(fetch).toHaveBeenNthCalledWith(1, expect.stringContaining("/admin/deployment/update-check"), expect.objectContaining({ method: "POST", body: '{"force":false}', signal: expect.any(AbortSignal) }));
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox"), "uv");
    expect(screen.getByText('uv tool install --force "dtxwiki==0.2.0"')).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Check now" }));
    expect(await screen.findByText("Check failed")).toBeInTheDocument();
    expect(screen.queryByText('uv tool install --force "dtxwiki==0.2.0"')).not.toBeInTheDocument();
    expect(screen.getByText("0.2.0")).toBeInTheDocument();
    expect(screen.getByText(/Last successful check:/)).not.toHaveTextContent("Not yet checked");
    expect(fetch).toHaveBeenNthCalledWith(2, expect.any(String), expect.objectContaining({ body: '{"force":true}' }));
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText('uv tool install --force "dtxwiki==0.2.0"')).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check now" })).toBeEnabled();
    expect(fetch).toHaveBeenNthCalledWith(3, expect.any(String), expect.objectContaining({ body: '{"force":true}' }));
  });

  it("blocks disabled checks and avoids pinned commands for unverified or older releases", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    const { rerender } = render(card({ ...FRESH, check_status: "disabled", update_available: false }));
    expect(screen.getByRole("button", { name: "Check now" })).toBeDisabled();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole("combobox"), "pip");
    expect(screen.queryByText(/^python -m pip/)).not.toBeInTheDocument();
    rerender(card({ ...FRESH, current_version: "0.3.0", update_available: false }));
    expect(screen.queryByText(/^python -m pip/)).not.toBeInTheDocument();
    expect(screen.getByText(/No newer release has been confirmed/)).toBeInTheDocument();
    rerender(card({ ...FRESH, latest_version: '0.2.0";echo bad', update_available: true }));
    expect(screen.queryByText(/^python -m pip/)).not.toBeInTheDocument();
    expect(screen.queryByText("New release")).not.toBeInTheDocument();
    rerender(card({ ...FRESH, current_version: "development", update_available: true }));
    expect(screen.queryByText(/^python -m pip/)).not.toBeInTheDocument();
    expect(screen.queryByText("New release")).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps check actions busy and ignores aborted responses after replacement or unmount", async () => {
    const resolvers: Array<(value: Response) => void> = [];
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise((done) => { resolvers.push(done); }));
    const { rerender, unmount } = render(card({ ...FRESH, stale: true }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const signal = fetch.mock.calls[0][1]?.signal;
    expect(screen.getByRole("button", { name: "Checking…" })).toBeDisabled();
    rerender(card(FRESH));
    expect(signal?.aborted).toBe(true);
    expect(screen.getByRole("button", { name: "Check now" })).toBeEnabled();
    await act(async () => { resolvers[0](response({ ...FRESH, latest_version: "99.0.0" })); });
    expect(screen.getByText("0.2.0")).toBeInTheDocument();
    expect(screen.queryByText("99.0.0")).not.toBeInTheDocument();
    rerender(card({ ...FRESH, stale: true }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    unmount();
    expect(fetch.mock.calls[1][1]?.signal?.aborted).toBe(true);
    await act(async () => { resolvers[1](response(FRESH)); });
  });
});
