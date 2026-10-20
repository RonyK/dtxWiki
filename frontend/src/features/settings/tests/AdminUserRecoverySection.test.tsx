import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { axe } from "jest-axe";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AUTH_EXPIRED_EVENT } from "../../../shared/api/client";
import { setLocale } from "../../../shared/i18n";
import { readAppStyles } from "../../../test-support/appStyles";
import { AdminUserRecoverySection } from "../AdminUserRecoverySection";

const BASE = "http://127.0.0.1:21080";
const USER_ID = "00000000-0000-0000-0000-000000000101";
const ISSUED_LINK_TOKEN = "dtxr_1_UNIT_ONETIME_SECRET";
const ISSUED_LINK_URL = `https://wiki.example.com/reset-password?token=${ISSUED_LINK_TOKEN}`;
const MASKED_LINK_URL = "https://wiki.example.com/reset-password?token=\u2026";

describe("AdminUserRecoverySection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    setLocale("en", { persist: false });
  });

  it("shows shared progress only while checking server recovery availability", async () => {
    let rejectCapabilities!: (reason: Error) => void;
    const pendingCapabilities = new Promise<Response>((_resolve, reject) => {
      rejectCapabilities = reject;
    });
    vi.spyOn(globalThis, "fetch").mockReturnValue(pendingCapabilities);
    render(<AdminUserRecoverySection />);

    const status = screen.getByText("Checking server recovery availability…")
      .closest('[role="status"]');
    expect(status).toHaveTextContent("Checking server recovery availability…");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status?.querySelector(".spinner")).toHaveAttribute("aria-hidden", "true");

    await act(async () => {
      rejectCapabilities(new Error("Unavailable"));
      await pendingCapabilities.catch(() => undefined);
    });
    expect(await screen.findByText(
      "Recovery availability could not be confirmed. Reload before issuing a reset.",
    )).toBeInTheDocument();
    expect(screen.queryByText("Checking server recovery availability…"))
      .not.toBeInTheDocument();
  });

  it("keeps the directory private until reauthentication and relocks when proof expires", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/auth/capabilities`) {
          return jsonResponse(authCapabilities());
        }
        if (url === `${BASE}/api/v1/auth/reauthenticate`) {
          const body = JSON.parse(String(init?.body));
          if (body.current_password === "wrong-password") {
            return jsonResponse(
              {
                error_code: "invalid_credentials",
                message: "invalid credentials",
                request_id: "request-wrong-password",
                warnings: [],
              },
              401,
            );
          }
          return jsonResponse(reauthResponse());
        }
        if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
          return jsonResponse(
            {
              error_code: "recent_reauthentication_required",
              message: "recent verification required",
              request_id: "request-proof-expired",
              warnings: [],
            },
            401,
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    render(<AdminUserRecoverySection />);

    expect(
      screen.queryByRole("searchbox", { name: "Search users" }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        input.toString().startsWith(`${BASE}/api/v1/admin/users?`),
      ),
    ).toBe(false);

    await user.type(
      screen.getByLabelText("Your current password"),
      "wrong-password",
    );
    await user.click(
      screen.getByRole("button", { name: "Unlock user search" }),
    );

    expect(
      await screen.findByText("Your password was not accepted. Try again."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("searchbox", { name: "Search users" }),
    ).not.toBeInTheDocument();

    await user.type(
      screen.getByLabelText("Your current password"),
      "admin-current-password",
    );
    await user.click(
      screen.getByRole("button", { name: "Unlock user search" }),
    );
    const searchbox = await screen.findByLabelText(/Search users/);
    await user.type(searchbox, "alex@example.com");
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(
      await screen.findByText(
        "Your recent verification expired. Re-enter your password to search users again.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("searchbox", { name: "Search users" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Alex Kim")).not.toBeInTheDocument();
  });

  it("issues and reveals a link once, sends revocation intent, and cannot reopen it", async () => {
    const user = userEvent.setup();
    const resetUrl = "https://wiki.example.com/reset-password?token=dtxr_once";
    // What the dialog is allowed to put on screen: origin and path, secret
    // redacted. See the masking test below for why.
    const maskedUrl = "https://wiki.example.com/reset-password?token=\u2026";
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/auth/reauthenticate`) {
          return jsonResponse(reauthResponse());
        }
        if (url === `${BASE}/api/v1/auth/capabilities`) {
          return jsonResponse(authCapabilities());
        }
        if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
          return jsonResponse(userSearchResponse(true));
        }
        if (
          url === `${BASE}/api/v1/admin/users/${USER_ID}/password-reset` &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            data: {
              user_id: USER_ID,
              delivery: "link",
              expires_at: "2026-08-01T03:30:00Z",
              reset_url: resetUrl,
              revoke_api_tokens: true,
            },
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    const { container } = render(<AdminUserRecoverySection />);
    await searchForUser(user, "alex@example.com");
    fetchMock.mockClear();

    const linkAction = await screen.findByRole("button", {
      name: "Issue one-time link",
    });
    await user.click(linkAction);

    let confirmDialog = screen.getByRole("dialog", {
      name: "Issue a password reset?",
    });
    expect(confirmDialog.parentElement).toBe(document.body);
    expect(container.querySelector(".modal-backdrop")).toBeNull();
    const ordinaryMode = within(confirmDialog).getByRole("radio", {
      name: /Ordinary recovery/,
    });
    const cancelButton = within(confirmDialog).getByRole("button", {
      name: "Cancel",
    });
    expect(ordinaryMode).toHaveFocus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(cancelButton).toHaveFocus();
    await user.keyboard("{Tab}");
    expect(ordinaryMode).toHaveFocus();

    fireEvent.mouseDown(confirmDialog);
    expect(
      screen.queryByRole("dialog", { name: "Issue a password reset?" }),
    ).not.toBeInTheDocument();
    expect(linkAction).toHaveFocus();

    await user.click(linkAction);
    confirmDialog = screen.getByRole("dialog", {
      name: "Issue a password reset?",
    });
    expect(
      within(confirmDialog).getByRole("checkbox", {
        name: /Also revoke all PAT\/MCP tokens/,
      }),
    ).not.toBeChecked();
    expect(
      within(confirmDialog).getByText("Issue one-time link"),
    ).toBeInTheDocument();
    await user.click(
      within(confirmDialog).getByRole("radio", {
        name: /Suspected account compromise/,
      }),
    );
    expect(
      within(confirmDialog).getByRole("checkbox", {
        name: /Also revoke all PAT\/MCP tokens/,
      }),
    ).toBeChecked();
    await user.type(
      within(confirmDialog).getByLabelText(/Your current password/),
      "admin-current-password",
    );
    await user.click(
      within(confirmDialog).getByRole("button", {
        name: "Issue password reset",
      }),
    );

    const privilegedCalls = fetchMock.mock.calls.filter(([input]) => {
      const url = input.toString();
      return (
        url === `${BASE}/api/v1/auth/reauthenticate` ||
        url === `${BASE}/api/v1/admin/users/${USER_ID}/password-reset`
      );
    });
    expect(privilegedCalls.map(([input]) => input.toString())).toEqual([
      `${BASE}/api/v1/auth/reauthenticate`,
      `${BASE}/api/v1/admin/users/${USER_ID}/password-reset`,
    ]);
    expect(JSON.parse(String(privilegedCalls[0]?.[1]?.body))).toEqual({
      current_password: "admin-current-password",
    });
    expect(JSON.parse(String(privilegedCalls[1]?.[1]?.body))).toEqual({
      delivery: "link",
      revoke_api_tokens: true,
    });

    const linkDialog = await screen.findByRole("dialog", {
      name: "Copy this recovery link now",
    });
    expect(linkDialog.parentElement).toBe(document.body);
    expect(within(linkDialog).getByDisplayValue(maskedUrl)).toHaveFocus();
    expect(
      within(linkDialog).getByText(/Closing this dialog permanently hides it/),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");
    fireEvent.mouseDown(linkDialog);
    expect(
      screen.getByRole("dialog", { name: "Copy this recovery link now" }),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue(maskedUrl)).toBeInTheDocument();
    await user.click(
      within(linkDialog).getByRole("button", { name: "Close and hide link" }),
    );

    expect(screen.queryByDisplayValue(maskedUrl)).not.toBeInTheDocument();
    expect(linkAction).toHaveFocus();
    expect(
      screen.getByText(/The one-time link was hidden/),
    ).toBeInTheDocument();

    await user.click(linkAction);
    expect(screen.queryByDisplayValue(maskedUrl)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Your current password/)).toHaveValue("");
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "Issue a password reset?" }),
    ).not.toBeInTheDocument();
    expect(linkAction).toHaveFocus();
  }, 15_000);

  it("masks the one-time link in the DOM and keeps the full URL for the clipboard", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    const linkDialog = await openOneTimeLinkDialog(user, ISSUED_LINK_URL);

    // The credential must not be reachable from the rendered document at all.
    // Playwright writes an `error-context.md` for every failed test whose
    // accessibility snapshot prints form-control *values* verbatim, and the
    // HTML reporter copies that file into `playwright-report/data/`; `.value`
    // is exactly what that snapshot reads. A screen share leaks it the same
    // way, which is why this is a product fix and not a test-only one.
    expect(inputsCarrying(ISSUED_LINK_TOKEN)).toEqual([]);
    expect(document.body.innerHTML.includes(ISSUED_LINK_TOKEN)).toBe(false);
    expect(within(linkDialog).getByLabelText("Recovery link")).toHaveValue(
      MASKED_LINK_URL,
    );

    await user.click(
      within(linkDialog).getByRole("button", { name: "Copy link" }),
    );

    // The clipboard is the one place the whole thing is allowed to go.
    expect(writeText).toHaveBeenCalledWith(ISSUED_LINK_URL);
    expect(
      await within(linkDialog).findByText("Recovery link copied."),
    ).toBeInTheDocument();
    expect(inputsCarrying(ISSUED_LINK_TOKEN)).toEqual([]);
    expect(document.body.innerHTML.includes(ISSUED_LINK_TOKEN)).toBe(false);
  }, 15_000);

  it("copies through a transient node without rendering the link when the Clipboard API is missing", async () => {
    const user = userEvent.setup();
    // A self-hosted deployment on plain HTTP: no secure context, so
    // `navigator.clipboard` does not exist and the admin still has to deliver
    // the link. The fallback must not undo the masking.
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: false,
    });
    const carried: string[] = [];
    const execCommand = vi.fn(() => {
      carried.push(
        ...Array.from(document.querySelectorAll("textarea")).map(
          (node) => node.value,
        ),
      );
      return true;
    });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand,
    });

    try {
      const linkDialog = await openOneTimeLinkDialog(user, ISSUED_LINK_URL);
      await user.click(
        within(linkDialog).getByRole("button", { name: "Copy link" }),
      );

      expect(carried).toEqual([ISSUED_LINK_URL]);
      expect(
        await within(linkDialog).findByText("Recovery link copied."),
      ).toBeInTheDocument();
      // The carrier is created and removed inside one synchronous turn, so
      // nothing that reads the document between renders can see it.
      expect(document.querySelectorAll("textarea")).toHaveLength(0);
      expect(inputsCarrying(ISSUED_LINK_TOKEN)).toEqual([]);
      expect(document.body.innerHTML.includes(ISSUED_LINK_TOKEN)).toBe(false);
    } finally {
      Reflect.deleteProperty(document, "execCommand");
    }
  }, 15_000);

  it("reveals the link on screen only after every programmatic copy path has failed", async () => {
    const user = userEvent.setup();
    // Deliberate, and the only rung of the ladder that puts the secret on
    // screen: without it an operator on a browser with neither copy path can
    // never deliver the link, which strands the locked-out user. It takes an
    // explicit click, and it is why the masking above is the default rather
    // than the only state.
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: false,
    });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: vi.fn(() => false),
    });

    try {
      const linkDialog = await openOneTimeLinkDialog(user, ISSUED_LINK_URL);
      expect(inputsCarrying(ISSUED_LINK_TOKEN)).toEqual([]);

      await user.click(
        within(linkDialog).getByRole("button", { name: "Copy link" }),
      );

      expect(
        await within(linkDialog).findByText(
          "Link selected. Press Ctrl+C to copy.",
        ),
      ).toBeInTheDocument();
      const field = within(linkDialog).getByLabelText("Recovery link");
      expect(field).toHaveValue(ISSUED_LINK_URL);
      expect(field).toHaveFocus();

      // Clicking Copy again hands the selection back rather than doing
      // nothing, which is all it can still do once the link is on screen.
      within(linkDialog)
        .getByRole("button", { name: "Close and hide link" })
        .focus();
      expect(field).not.toHaveFocus();
      await user.click(
        within(linkDialog).getByRole("button", { name: "Copy link" }),
      );
      expect(field).toHaveFocus();
    } finally {
      Reflect.deleteProperty(document, "execCommand");
    }
  }, 15_000);

  it("blocks Escape and backdrop dismissal while issuing a recovery reset", async () => {
    const user = userEvent.setup();
    let resolveReset!: (response: Response) => void;
    const pendingReset = new Promise<Response>((resolve) => {
      resolveReset = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/reauthenticate`) {
        return jsonResponse(reauthResponse());
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse(authCapabilities());
      }
      if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
        return jsonResponse(userSearchResponse(true));
      }
      if (
        url === `${BASE}/api/v1/admin/users/${USER_ID}/password-reset` &&
        init?.method === "POST"
      ) {
        return pendingReset;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<AdminUserRecoverySection />);
    await searchForUser(user, "alex@example.com");
    const recoveryAction = screen.getByRole("button", {
      name: "Send recovery email",
    });
    await user.click(recoveryAction);
    const dialog = screen.getByRole("dialog", {
      name: "Issue a password reset?",
    });
    await user.type(
      within(dialog).getByLabelText(/Your current password/),
      "admin-current-password",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Issue password reset" }),
    );

    expect(
      await within(dialog).findByRole("button", { name: "Issuing reset…" }),
    ).toBeDisabled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    fireEvent.mouseDown(dialog);
    expect(
      screen.getByRole("dialog", { name: "Issue a password reset?" }),
    ).toBeInTheDocument();

    resolveReset(
      jsonResponse({
        data: {
          user_id: USER_ID,
          delivery: "email",
          expires_at: "2026-08-01T03:30:00Z",
          reset_url: null,
          revoke_api_tokens: false,
        },
      }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "Issue a password reset?" }),
      ).not.toBeInTheDocument();
    });
    expect(recoveryAction).toHaveFocus();
  }, 15_000);

  it("uses the global full-viewport dialog layer for recovery modals", () => {
    const styles = readAppStyles();
    const backdropRule = styles.match(/\.modal-backdrop\s*\{([^}]*)\}/)?.[1];

    expect(styles).toContain("--z-dialog: 100;");
    expect(backdropRule).toContain("position: fixed;");
    expect(backdropRule).toContain("inset: 0;");
    expect(backdropRule).toContain("z-index: var(--z-dialog);");
  });

  it("keeps a failed current-password challenge in the dialog without expiring the session", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-1");
    const onExpired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/auth/reauthenticate`) {
          const body = JSON.parse(String(init?.body));
          if (body.current_password === "wrong-password") {
            return jsonResponse(
              {
                error_code: "invalid_credentials",
                message: "invalid credentials",
                request_id: "request-wrong-password",
                warnings: [],
              },
              401,
            );
          }
          return jsonResponse(reauthResponse());
        }
        if (url === `${BASE}/api/v1/auth/capabilities`) {
          return jsonResponse(authCapabilities());
        }
        if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
          return jsonResponse(userSearchResponse(true));
        }
        if (
          url === `${BASE}/api/v1/admin/users/${USER_ID}/password-reset` &&
          init?.method === "POST"
        ) {
          throw new Error(
            "Password reset must not run after failed reauthentication",
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    try {
      render(<AdminUserRecoverySection />);
      await searchForUser(user, "alex@example.com");
      await user.click(
        await screen.findByRole("button", { name: "Send recovery email" }),
      );
      await user.type(
        screen.getByLabelText(/Your current password/),
        "wrong-password",
      );
      await user.click(
        screen.getByRole("button", { name: "Issue password reset" }),
      );

      expect(
        await screen.findByText(
          "Your current password is incorrect. Try again.",
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("dialog", { name: "Issue a password reset?" }),
      ).toBeInTheDocument();
      expect(screen.getByLabelText(/Your current password/)).toHaveFocus();
      expect(screen.getByLabelText(/Your current password/)).toHaveValue("");
      expect(onExpired).not.toHaveBeenCalled();
      expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
        "workspace-1",
      );
      expect(
        fetchMock.mock.calls.filter(
          ([input]) =>
            input.toString() ===
            `${BASE}/api/v1/admin/users/${USER_ID}/password-reset`,
        ),
      ).toHaveLength(0);
    } finally {
      window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
    }
  });

  it("never falls back from failed email delivery to revealing a link", async () => {
    const user = userEvent.setup();
    const issuedBodies: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/reauthenticate`) {
        return jsonResponse(reauthResponse());
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse(authCapabilities());
      }
      if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
        return jsonResponse(userSearchResponse(true));
      }
      if (
        url === `${BASE}/api/v1/admin/users/${USER_ID}/password-reset` &&
        init?.method === "POST"
      ) {
        issuedBodies.push(JSON.parse(String(init.body)));
        return jsonResponse(
          {
            error_code: "password_recovery_unavailable",
            message: "mail provider rejected delivery",
            request_id: "request-mail-failed",
            warnings: [],
          },
          422,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<AdminUserRecoverySection />);
    await searchForUser(user, "alex@example.com");
    await user.click(
      await screen.findByRole("button", { name: "Send recovery email" }),
    );
    await user.type(
      screen.getByLabelText(/Your current password/),
      "admin-current-password",
    );
    await user.click(
      screen.getByRole("button", { name: "Issue password reset" }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: "Issue a password reset?",
    });
    expect(
      within(dialog).getByText(
        "That delivery method is unavailable. Close this dialog and explicitly choose an available recovery action.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Send recovery email")).toBeInTheDocument();
    expect(screen.queryByLabelText("Recovery link")).not.toBeInTheDocument();
    expect(issuedBodies).toHaveLength(1);
    expect(issuedBodies[0]).toMatchObject({ delivery: "email" });
  }, 15_000);

  it("queues verified-email delivery without revealing a link", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/auth/reauthenticate`) {
          return jsonResponse(reauthResponse());
        }
        if (url === `${BASE}/api/v1/auth/capabilities`) {
          return jsonResponse(authCapabilities());
        }
        if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
          return jsonResponse(userSearchResponse(true));
        }
        if (
          url === `${BASE}/api/v1/admin/users/${USER_ID}/password-reset` &&
          init?.method === "POST"
        ) {
          return jsonResponse({
            data: {
              user_id: USER_ID,
              delivery: "email",
              expires_at: "2026-08-01T03:30:00Z",
              reset_url: null,
              revoke_api_tokens: false,
            },
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    render(<AdminUserRecoverySection />);
    await searchForUser(user, "alex@example.com");
    await user.click(
      await screen.findByRole("button", { name: "Send recovery email" }),
    );
    await user.type(
      screen.getByLabelText(/Your current password/),
      "admin-current-password",
    );
    await user.click(
      screen.getByRole("button", { name: "Issue password reset" }),
    );

    expect(
      await screen.findByText("Recovery email queued for alex@example.com."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Recovery link")).not.toBeInTheDocument();
    const request = fetchMock.mock.calls.find(
      ([input]) =>
        input.toString() ===
        `${BASE}/api/v1/admin/users/${USER_ID}/password-reset`,
    );
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({
      delivery: "email",
      revoke_api_tokens: false,
    });
  }, 15_000);

  it("keeps delivery actions independent and disables email for an unverified address", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/reauthenticate`) {
        return jsonResponse(reauthResponse());
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse(authCapabilities());
      }
      if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
        return jsonResponse(userSearchResponse(false));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const { container } = render(<AdminUserRecoverySection />);
    await searchForUser(user, "alex@example.com");
    expect(
      await screen.findByRole("button", { name: "Send recovery email" }),
    ).toBeDisabled();
    const linkAction = screen.getByRole("button", {
      name: "Issue one-time link",
    });
    expect(linkAction).toBeEnabled();
    expect(
      screen.getByText(
        "Email delivery is unavailable because this address is not verified.",
      ),
    ).toBeInTheDocument();
    await user.click(linkAction);
    expect(
      screen.getByRole("dialog", { name: "Issue a password reset?" }),
    ).toBeInTheDocument();
    expect(
      within(
        screen.getByRole("dialog", { name: "Issue a password reset?" }),
      ).getByText("Issue one-time link"),
    ).toBeInTheDocument();
    expect((await axe(container)).violations).toEqual([]);
  });

  it("requires both a verified target and ready server mail capability", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/reauthenticate`) {
        return jsonResponse(reauthResponse());
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse(authCapabilities("admin_assisted"));
      }
      if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
        return jsonResponse(userSearchResponse(true));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<AdminUserRecoverySection />);
    await searchForUser(user, "alex@example.com");

    expect(
      screen.getByRole("button", { name: "Send recovery email" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Issue one-time link" }),
    ).toBeEnabled();
    expect(
      screen.getByText(
        "Email delivery is unavailable because server mail recovery is not ready.",
      ),
    ).toBeInTheDocument();
  });

  it("pages through protected search results without exposing an unbounded list", async () => {
    const user = userEvent.setup();
    const requestedUrls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/reauthenticate`) {
        return jsonResponse(reauthResponse());
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse(authCapabilities());
      }
      requestedUrls.push(url);
      if (url.includes("offset=25")) {
        return jsonResponse({
          data: {
            items: [
              {
                user_id: "00000000-0000-0000-0000-000000000102",
                email: "casey@example.com",
                display_name: "Casey Lee",
                status: "active",
                email_verified_at: null,
                workspaces: [],
              },
            ],
            total: 26,
            limit: 25,
            offset: 25,
          },
        });
      }
      if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
        const firstPage = userSearchResponse(true);
        firstPage.data.total = 26;
        return jsonResponse(firstPage);
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<AdminUserRecoverySection />);
    await searchForUser(user, "Research");
    expect(screen.getByText("1–1 of 26")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Casey Lee")).toBeInTheDocument();
    expect(screen.getByText("26–26 of 26")).toBeInTheDocument();
    expect(requestedUrls.at(-1)).toContain("offset=25");
    expect(screen.getByRole("button", { name: "Previous" })).toBeEnabled();
  });

  it("renders the independent recovery actions and compromise mode in Korean", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/reauthenticate`) {
        return jsonResponse(reauthResponse());
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse(authCapabilities());
      }
      if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
        return jsonResponse(userSearchResponse(true));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    setLocale("ko", { persist: false });
    render(<AdminUserRecoverySection />);

    expect(
      screen.getByRole("heading", { name: "계정 복구" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("사용자 검색")).not.toBeInTheDocument();
    await user.type(
      screen.getByLabelText("내 현재 비밀번호"),
      "admin-current-password",
    );
    await user.click(
      screen.getByRole("button", { name: "사용자 검색 잠금 해제" }),
    );
    await screen.findByLabelText(/사용자 검색/);
    expect(screen.getByText("사용자 검색")).toBeInTheDocument();
    expect(
      screen.getByText(/PAT\/MCP 토큰은 별도 자격 증명/),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText(/사용자 검색/), "alex@example.com");
    await user.click(screen.getByRole("button", { name: "검색" }));
    await screen.findByText("Alex Kim");
    expect(
      screen.getByRole("button", { name: "복구 메일 보내기" }),
    ).toBeEnabled();
    const linkAction = screen.getByRole("button", { name: "일회용 링크 발급" });
    expect(linkAction).toBeEnabled();
    await user.click(linkAction);
    expect(screen.getByRole("radio", { name: "일반 복구" })).toBeChecked();
    expect(
      screen.getByRole("radio", { name: "계정 탈취 의심" }),
    ).not.toBeChecked();
  });
});

/** Every rendered input whose live value carries `secret`. */
function inputsCarrying(secret: string) {
  return Array.from(document.querySelectorAll("input")).filter((field) =>
    field.value.includes(secret),
  );
}

function mockLinkIssuingFetch(resetUrl: string) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/reauthenticate`) {
        return jsonResponse(reauthResponse());
      }
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse(authCapabilities());
      }
      if (url.startsWith(`${BASE}/api/v1/admin/users?`)) {
        return jsonResponse(userSearchResponse(true));
      }
      if (
        url === `${BASE}/api/v1/admin/users/${USER_ID}/password-reset` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          data: {
            user_id: USER_ID,
            delivery: "link",
            expires_at: "2026-08-01T03:30:00Z",
            reset_url: resetUrl,
            revoke_api_tokens: true,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
}

async function openOneTimeLinkDialog(
  user: ReturnType<typeof userEvent.setup>,
  resetUrl: string,
) {
  mockLinkIssuingFetch(resetUrl);
  render(<AdminUserRecoverySection />);
  await searchForUser(user, "alex@example.com");
  await user.click(
    await screen.findByRole("button", { name: "Issue one-time link" }),
  );
  const confirmDialog = screen.getByRole("dialog", {
    name: "Issue a password reset?",
  });
  await user.type(
    within(confirmDialog).getByLabelText(/Your current password/),
    "admin-current-password",
  );
  await user.click(
    within(confirmDialog).getByRole("button", {
      name: "Issue password reset",
    }),
  );
  return screen.findByRole("dialog", {
    name: "Copy this recovery link now",
  });
}

async function searchForUser(
  user: ReturnType<typeof userEvent.setup>,
  query: string,
) {
  await user.type(
    screen.getByLabelText("Your current password"),
    "admin-current-password",
  );
  await user.click(screen.getByRole("button", { name: "Unlock user search" }));
  await screen.findByLabelText(/Search users/);
  await user.type(screen.getByLabelText(/Search users/), query);
  await user.click(screen.getByRole("button", { name: "Search" }));
  await screen.findByText("Alex Kim");
}

function reauthResponse() {
  return {
    data: {
      reauthenticated_until: "2026-08-01T03:05:00Z",
    },
  };
}

function userSearchResponse(verified: boolean) {
  return {
    data: {
      items: [
        {
          user_id: USER_ID,
          email: "alex@example.com",
          display_name: "Alex Kim",
          status: "active",
          email_verified_at: verified ? "2026-07-31T01:00:00Z" : null,
          workspaces: [
            {
              workspace_id: "00000000-0000-0000-0000-000000000201",
              slug: "research",
              title: "Research",
              role: "editor",
            },
          ],
        },
      ],
      total: 1,
      limit: 25,
      offset: 0,
    },
  };
}

function authCapabilities(
  mode: "email" | "admin_assisted" | "disabled" = "email",
) {
  return {
    data: {
      signup_enabled: false,
      password_recovery: {
        mode,
        admin_assisted: mode !== "disabled",
      },
    },
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
