import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../shared/auth/AuthContext";
import { setLocale } from "../../../shared/i18n";
import { ResetPasswordScreen } from "../ResetPasswordScreen";

const BASE = "http://127.0.0.1:21080";
const STATUS_URL = `${BASE}/api/v1/auth/password-reset/status`;

describe("ResetPasswordScreen", () => {
  beforeEach(() => {
    setLocale("en", { persist: false });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("submits the one-time token and returns to sign in without a session", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => jsonResponse({ data: { reset: true } }));
    renderReset("/reset-password#token=dtxr_0_one-time-token");

    await user.type(
      screen.getByLabelText("New password"),
      "replacement-password",
    );
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "replacement-password",
    );
    const revokeTokens = screen.getByRole("checkbox", {
      name: "Also revoke all API/MCP tokens",
    });
    expect(revokeTokens).not.toBeChecked();
    await user.click(revokeTokens);
    await user.click(screen.getByRole("button", { name: "Reset password" }));

    expect(await screen.findByText("Login destination")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // M-2: the status check is a POST with the token in the body. The URL it
    // replaced (`status?token=`) was written into every access log.
    expect(fetchMock.mock.calls[0][0]).toBe(STATUS_URL);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ token: "dtxr_0_one-time-token" }),
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      `${BASE}/api/v1/auth/password-reset/confirm`,
    );
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      method: "POST",
      credentials: "include",
      body: JSON.stringify({
        token: "dtxr_0_one-time-token",
        new_password: "replacement-password",
        revoke_api_tokens: true,
      }),
    });
  });

  it("takes the token out of the address bar and keeps using it from memory (M-2)", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => jsonResponse({ data: { reset: true } }));
    renderReset("/reset-password#token=dtxr_0_address-bar-token", {
      probeLocation: true,
    });

    await waitFor(() =>
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        /^\/reset-password$/,
      ),
    );
    expect(
      screen.getByTestId("current-location").textContent,
    ).not.toContain("address-bar-token");

    await user.type(screen.getByLabelText("New password"), "replacement-password");
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "replacement-password",
    );
    await user.click(screen.getByRole("button", { name: "Reset password" }));

    expect(await screen.findByText("Login destination")).toBeInTheDocument();
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify({
        token: "dtxr_0_address-bar-token",
        new_password: "replacement-password",
        revoke_api_tokens: false,
      }),
    });
    // No request URL ever carried the token.
    for (const [url] of fetchMock.mock.calls) {
      expect(String(url)).not.toContain("address-bar-token");
    }
  });

  it("still accepts a legacy ?token= link, strips it, and keeps other parameters", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: { valid: true } }));
    renderReset("/reset-password?token=dtxr_0_legacy-token&lang=ko", {
      probeLocation: true,
    });

    await waitFor(() =>
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        /^\/reset-password\?lang=ko$/,
      ),
    );
    expect(await screen.findByLabelText("New password")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(STATUS_URL);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ token: "dtxr_0_legacy-token" }),
    });
  });

  it("prefers the fragment token when a link carries both forms", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: { valid: true } }));
    renderReset(
      "/reset-password?token=dtxr_0_query-token#token=dtxr_0_fragment-token",
      { probeLocation: true },
    );

    await waitFor(() =>
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        /^\/reset-password$/,
      ),
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      body: JSON.stringify({ token: "dtxr_0_fragment-token" }),
    });
  });

  it("forces API/MCP token revocation for a protected compromise-recovery token", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => jsonResponse({ data: { reset: true } }));
    renderReset("/reset-password#token=dtxr_1_compromise-token");

    const revokeTokens = screen.getByRole("checkbox", {
      name: "Also revoke all API/MCP tokens",
    });
    expect(revokeTokens).toBeChecked();
    expect(revokeTokens).toBeDisabled();
    expect(
      screen.getByText(/issued for suspected account compromise/),
    ).toBeInTheDocument();
    await user.type(
      screen.getByLabelText("New password"),
      "replacement-password",
    );
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "replacement-password",
    );
    await user.click(screen.getByRole("button", { name: "Reset password" }));

    expect(await screen.findByText("Login destination")).toBeInTheDocument();
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify({
        token: "dtxr_1_compromise-token",
        new_password: "replacement-password",
        revoke_api_tokens: true,
      }),
    });
  });

  it("explains forced compromise recovery in Korean", () => {
    setLocale("ko", { persist: false });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: { valid: true } }),
    );
    renderReset("/reset-password#token=dtxr_1_compromise-token");

    expect(
      screen.getByText(/계정 탈취 의심 복구용으로 발급/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "API/MCP 토큰도 모두 폐기" }),
    ).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "API/MCP 토큰도 모두 폐기" }),
    ).toBeDisabled();
  });

  it("does not render server detail for an invalid link, detected on load (M-23)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        {
          error_code: "invalid_password_reset_token",
          message: "server detail must not be rendered",
        },
        400,
      ),
    );
    renderReset("/reset-password#token=dtxr_invalid-token");

    expect(
      await screen.findByText(
        "This password reset link is invalid, expired, or already used.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("server detail must not be rendered"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Password reset link unavailable" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "This password reset link cannot be used to choose a new password.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  });

  it.each([
    [
      "password_reset_token_expired",
      "This password reset link has expired. Request a new link to continue.",
    ],
    [
      "password_reset_token_used",
      "This password reset link has already been used. Request a new link if you still cannot sign in.",
    ],
  ])(
    "renders the %s lifecycle state as soon as the page loads (M-23)",
    async (errorCode, expectedMessage) => {
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse(
          {
            error_code: errorCode,
            message: "server detail must not be rendered",
          },
          400,
        ),
      );
      renderReset("/reset-password#token=dtxr_state-token");

      expect(await screen.findByText(expectedMessage)).toBeInTheDocument();
      expect(
        screen.getByRole("heading", {
          name: "Password reset link unavailable",
        }),
      ).toBeInTheDocument();
      expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
      expect(
        screen.queryByText("server detail must not be rendered"),
      ).not.toBeInTheDocument();
    },
  );

  it.each([
    [429, "Too many reset attempts were made. Wait a moment, then try again."],
    [
      503,
      "The server could not reset your password right now. Your link may still be valid; try again.",
    ],
  ])("keeps HTTP %s distinct from an invalid link", async (status, message) => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ error_code: "internal_error", message: "hidden" }, status),
    );
    renderReset("/reset-password#token=dtxr_transient-token");

    await user.type(screen.getByLabelText("New password"), "replacement-password");
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "replacement-password",
    );
    await user.click(screen.getByRole("button", { name: "Reset password" }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(
      screen.queryByText(
        "This password reset link is invalid, expired, or already used.",
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Reset password" }),
    ).toBeEnabled();
  });

  it("keeps an offline failure retryable", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ data: { valid: true } }))
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(jsonResponse({ data: { reset: true } }));
    renderReset("/reset-password#token=dtxr_offline-token");
    await user.type(screen.getByLabelText("New password"), "replacement-password");
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "replacement-password",
    );
    await user.click(screen.getByRole("button", { name: "Reset password" }));

    expect(
      await screen.findByText(
        "The server could not be reached. Check your connection and try again.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset password" }));
    expect(await screen.findByText("Login destination")).toBeInTheDocument();
    // The retry still had the token although the address bar no longer does.
    expect(fetchMock.mock.calls[2][1]).toMatchObject({
      body: JSON.stringify({
        token: "dtxr_offline-token",
        new_password: "replacement-password",
        revoke_api_tokens: false,
      }),
    });
  });

  it("does not submit a missing token or mismatched passwords", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: { valid: true } }));
    const { unmount } = renderReset("/reset-password");
    // Opened (or reloaded) without the link: the link is cleared from the
    // address bar once read, so the page asks for it again instead of calling
    // a still-valid link dead.
    expect(
      screen.getByText(
        "Open your password reset link again. For your security, this page removes the link from the address bar as soon as it opens, so reloading the page cannot reuse it.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "This password reset link is invalid, expired, or already used.",
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Password reset link unavailable" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "This password reset link cannot be used to choose a new password.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "This one-time link lets you choose a new password. Completing it signs out every browser session.",
      ),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    unmount();

    renderReset("/reset-password#token=dtxr_valid-token");
    await user.type(
      screen.getByLabelText("New password"),
      "replacement-password",
    );
    await user.type(
      screen.getByLabelText("Confirm new password"),
      "different-password",
    );
    await user.click(screen.getByRole("button", { name: "Reset password" }));
    expect(
      await screen.findByText("New passwords do not match."),
    ).toBeInTheDocument();
    // Only the on-load status check ran; the mismatch blocked confirmPasswordReset.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(STATUS_URL);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ token: "dtxr_valid-token" }),
    });
  });

  it("offers a new reset request when email recovery is available", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          signup_enabled: true,
          password_recovery: { mode: "email", admin_assisted: false },
        },
      }),
    );
    renderReset("/reset-password", { loadInstanceCapabilities: true });

    const requestNewLink = await screen.findByRole("link", {
      name: "Request a new reset link",
    });
    expect(requestNewLink).toHaveAttribute("href", "/forgot-password");
    await userEvent.click(requestNewLink);
    expect(await screen.findByText("Recovery destination")).toBeInTheDocument();
  });
});

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="current-location">
      {`${location.pathname}${location.search}${location.hash}`}
    </output>
  );
}

function renderReset(
  initialEntry: string,
  {
    loadInstanceCapabilities = false,
    probeLocation = false,
  }: { loadInstanceCapabilities?: boolean; probeLocation?: boolean } = {},
) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AuthProvider
        loadInstanceCapabilities={loadInstanceCapabilities}
        skipInitialSessionCheck
      >
        <Routes>
          <Route
            path="/reset-password"
            element={
              <>
                <ResetPasswordScreen />
                {probeLocation ? <LocationProbe /> : null}
              </>
            }
          />
          <Route path="/forgot-password" element={<div>Recovery destination</div>} />
          <Route path="/login" element={<div>Login destination</div>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}
