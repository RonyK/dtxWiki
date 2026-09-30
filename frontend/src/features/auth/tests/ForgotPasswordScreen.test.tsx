import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../shared/auth/AuthContext";
import { setLocale } from "../../../shared/i18n";
import { ForgotPasswordScreen } from "../ForgotPasswordScreen";

const BASE = "http://127.0.0.1:21080";

describe("ForgotPasswordScreen", () => {
  beforeEach(() => {
    setLocale("en", { persist: false });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each(["admin_assisted", "disabled"] as const)(
    "hides the recovery form and names the server operator in %s mode",
    async (mode) => {
      const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse({
          data: {
            signup_enabled: false,
            password_recovery: {
              mode,
              admin_assisted: mode === "admin_assisted",
            },
          },
        }),
      );

      render(
        <MemoryRouter initialEntries={["/forgot-password"]}>
          <AuthProvider loadInstanceCapabilities skipInitialSessionCheck>
            <ForgotPasswordScreen />
          </AuthProvider>
        </MemoryRouter>,
      );

      expect(
        await screen.findByText(
          "Email recovery is not configured on this server. Contact the server operator.",
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/There is no public account search/),
      ).toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: "Email" })).not.toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/auth/capabilities`,
        expect.objectContaining({ credentials: "include" }),
      );
    },
  );
});

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}
