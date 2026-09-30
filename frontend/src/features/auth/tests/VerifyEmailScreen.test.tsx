import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import { VerifyEmailScreen } from "../VerifyEmailScreen";

const CONFIRM_URL =
  "http://127.0.0.1:21080/api/v1/auth/email-verification/confirm";

describe("VerifyEmailScreen", () => {
  beforeEach(() => setLocale("en", { persist: false }));
  afterEach(() => vi.restoreAllMocks());

  it("posts the fragment token once and clears it from the address bar (M-2)", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: { verified: true } }));
    renderVerify("/verify-email#token=dtxe_fragment-token");

    expect(await screen.findByText("Your email is verified.")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        /^\/verify-email$/,
      ),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(CONFIRM_URL);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ token: "dtxe_fragment-token" }),
    });
  });

  it("still accepts a legacy ?token= link and clears it from the address bar", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: { verified: true } }));
    renderVerify("/verify-email?token=dtxe_legacy-token");

    expect(await screen.findByText("Your email is verified.")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        /^\/verify-email$/,
      ),
    );
    expect(fetchMock.mock.calls[0][0]).toBe(CONFIRM_URL);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ token: "dtxe_legacy-token" }),
    });
  });

  it.each([
    [429, "Too many verification attempts were made. Wait a moment, then try again."],
    [
      503,
      "The server could not verify your email right now. Your link may still be valid; try again.",
    ],
  ])("keeps HTTP %s retryable and distinct from invalid", async (status, message) => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(errorResponse(status))
      .mockResolvedValueOnce(jsonResponse({ data: { verified: true } }));
    renderVerify();

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(
      screen.queryByText(
        "This verification link is invalid, expired, or already used.",
      ),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Your email is verified.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // The retry runs after the token left the address bar, from memory.
    expect(screen.getByTestId("current-location")).toHaveTextContent(
      /^\/verify-email$/,
    );
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify({ token: "dtxe_token-for-retries" }),
    });
  });

  it("offers a reissue path to /account for an invalid/expired token (UI-15)", async () => {
    // No token at all renders "invalid" directly (see the component's
    // initial state), which is also what a used/expired token settles into
    // via confirmEmailVerification -- this is the single most common
    // failure, and until now had no way out except "back to sign in".
    const fetchMock = vi.spyOn(globalThis, "fetch");
    render(
      <MemoryRouter initialEntries={["/verify-email"]}>
        <VerifyEmailScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "This verification link is invalid, expired, or already used.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Sign in to request a new link" }),
    ).toHaveAttribute("href", "/account");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    [429, "Too many verification attempts were made. Wait a moment, then try again."],
  ])(
    "does not offer the reissue link for a retryable (non-invalid) failure",
    async (status, message) => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(errorResponse(status));
      renderVerify();

      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(
        screen.queryByRole("link", { name: "Sign in to request a new link" }),
      ).not.toBeInTheDocument();
    },
  );

  it("reports an offline request separately and retries", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(jsonResponse({ data: { verified: true } }));
    renderVerify();

    expect(
      await screen.findByText(
        "The server could not be reached. Check your connection and try again.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Your email is verified.")).toBeInTheDocument();
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

function renderVerify(initialEntry = "/verify-email#token=dtxe_token-for-retries") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <VerifyEmailScreen />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function errorResponse(status: number) {
  return jsonResponse(
    {
      error_code: status === 429 ? "rate_limited" : "internal_error",
      message: "server detail",
      request_id: "request-id",
      warnings: [],
    },
    status,
  );
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}
