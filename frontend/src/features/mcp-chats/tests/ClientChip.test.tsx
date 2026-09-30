import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ClientChip,
  ClientIdentity,
  boundedClientIdentity,
  normalizeClientIdentity,
} from "../ClientChip";

afterEach(() => {
  vi.restoreAllMocks();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: undefined,
  });
  Object.defineProperty(window, "isSecureContext", {
    configurable: true,
    value: true,
  });
});

describe("ClientChip", () => {
  it("uses only a server-projected brand hint", () => {
    render(
      <ClientChip
        client={{
          brand_hint: "synthetic-app",
          name: "Any display name",
        }}
      />,
    );

    expect(screen.getByLabelText("Any display name")).toHaveAttribute(
      "data-brand",
      "synthetic-app",
    );
  });

  it("does not infer a known app identity from a display name", () => {
    render(
      <ClientChip
        client={{
          brand_hint: null,
          name: "Claude-shaped unverified client",
        }}
      />,
    );

    expect(
      screen.getByLabelText("Claude-shaped unverified client"),
    ).toHaveAttribute("data-brand", "unknown");
  });

  it("bounds the accessible list identity and exposes neither scopes nor a native tooltip", () => {
    const name = "A".repeat(200);
    const { container } = render(
      <ClientChip
        client={{
          brand_hint: "oauth-client",
          name,
        }}
      />,
    );

    const chip = screen.getByLabelText(boundedClientIdentity(name, 48));
    expect(chip).not.toHaveAttribute("title");
    expect(chip).not.toHaveAccessibleName(name);
    expect(container).not.toHaveTextContent("mcp:read");
    expect(chip.getAttribute("aria-label")?.length).toBeLessThanOrEqual(48);
  });

  it("normalizes control and bidi characters before rendering identity text", () => {
    expect(normalizeClientIdentity("  OAuth\u202e client\nname  ")).toBe(
      "OAuth client name",
    );
  });

  it("keeps an emoji grapheme intact for the avatar and bounded label", () => {
    const name = `${"👩🏽‍💻".repeat(60)} 한국어 OAuth`;
    render(<ClientChip client={{ brand_hint: null, name }} />);

    const label = boundedClientIdentity(name, 48);
    const chip = screen.getByLabelText(label);
    expect(chip).toHaveTextContent("👩🏽‍💻");
    expect(label).toMatch(/…$/u);
    expect(label).not.toContain("�");
  });
});

describe("ClientIdentity", () => {
  it("uses a keyboard disclosure with a single bounded, stateful accessible name", async () => {
    const user = userEvent.setup();
    const name = "OAuth-".repeat(33).slice(0, 200);
    const { container } = render(
      <ClientIdentity
        client={{
          id: "client-1",
          brand_hint: null,
          name,
          scopes: ["mcp:read", "custom:review"],
        }}
      />,
    );

    const details = container.querySelector("details");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText(name)).not.toBeVisible();
    expect(container.querySelector(`[title="${name}"]`)).toBeNull();
    expect(container.querySelector(`[title="${boundedClientIdentity(name, 72)}"]`))
      .toBeNull();

    const summary = screen.getByLabelText(
      `Show full client details for ${boundedClientIdentity(name, 72)}`,
    );
    expect(summary).toHaveAccessibleName(
      `Show full client details for ${boundedClientIdentity(name, 72)}`,
    );

    // Native summary/details provides the keyboard disclosure contract; JSDOM
    // does not emulate Enter's default toggle, so activate it with a click.
    expect(summary.tagName).toBe("SUMMARY");
    await user.click(summary);

    expect(details).toHaveAttribute("open");
    expect(summary).toHaveAccessibleName(
      `Hide full client details for ${boundedClientIdentity(name, 72)}`,
    );
    expect(screen.getByText(name)).toBeVisible();
    expect(screen.getByText("mcp:read")).toBeVisible();
    expect(screen.getByText("Read workspace content")).toBeVisible();
    expect(screen.getByText("Additional client permission")).toBeVisible();
    expect(screen.getByText("client-1")).toBeVisible();
  });

  it("copies the full identifier and exposes a tooltip plus live success state", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    render(
      <ClientIdentity
        client={{
          id: "oauth-client-private-identifier",
          brand_hint: "oauth-client",
          name: "OAuth client",
          scopes: [],
        }}
      />,
    );

    await user.click(screen.getByLabelText("Show full client details for OAuth client"));
    const copy = screen.getByRole("button", { name: "Copy client identifier" });
    expect(copy).toHaveAttribute("title", "Copy client identifier");
    await user.click(copy);

    expect(writeText).toHaveBeenCalledWith("oauth-client-private-identifier");
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Client identifier copied",
    );
    expect(screen.getByRole("button", { name: "Client identifier copied" }))
      .toHaveAttribute("title", "Client identifier copied");
  });

  it("selects the visible identifier and explains why automatic copy failed", async () => {
    const user = userEvent.setup();
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: false,
    });
    render(
      <ClientIdentity
        client={{
          id: "client-select-me",
          brand_hint: null,
          name: "Local client",
          scopes: ["permission:with:a:very:long:unbroken:value"],
        }}
      />,
    );

    await user.click(screen.getByLabelText("Show full client details for Local client"));
    await user.click(screen.getByRole("button", { name: "Copy client identifier" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "this connection does not allow clipboard access",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "The identifier is selected; press Ctrl+C",
    );
    expect(window.getSelection()?.toString()).toBe("client-select-me");
  });

  it("keeps permission rejection reasoned instead of throwing an unhandled promise", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied raw detail")) },
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    render(
      <ClientIdentity
        client={{
          id: "client-denied",
          brand_hint: null,
          name: "Denied client",
          scopes: [],
        }}
      />,
    );

    await user.click(screen.getByLabelText("Show full client details for Denied client"));
    await user.click(screen.getByRole("button", { name: "Copy client identifier" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "browser permission was denied",
    );
    expect(screen.queryByText(/denied raw detail/)).toBeNull();
  });

  it("explains unsupported automatic copy while preserving the manual selection", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    render(
      <ClientIdentity
        client={{
          id: "client-unsupported",
          brand_hint: null,
          name: "Legacy browser client",
          scopes: [],
        }}
      />,
    );

    await user.click(
      screen.getByLabelText("Show full client details for Legacy browser client"),
    );
    await user.click(screen.getByRole("button", { name: "Copy client identifier" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "this browser does not support automatic copying",
    );
    expect(window.getSelection()?.toString()).toBe("client-unsupported");
  });

  it("shows copy unavailable when even the visible identifier cannot be selected", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    vi.spyOn(window, "getSelection").mockReturnValue(null);
    render(
      <ClientIdentity
        client={{
          id: "client-no-selection",
          brand_hint: null,
          name: "No selection client",
          scopes: [],
        }}
      />,
    );

    await user.click(
      screen.getByLabelText("Show full client details for No selection client"),
    );
    await user.click(screen.getByRole("button", { name: "Copy client identifier" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Copy unavailable: the identifier could not be selected",
    );
  });
});
