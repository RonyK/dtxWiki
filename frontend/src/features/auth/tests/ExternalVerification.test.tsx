import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import { IdentityVerificationPanel } from "../IdentityVerificationPanel";
import { IdentityVerificationScreen } from "../IdentityVerificationScreen";

const json = (data: unknown) => new Response(JSON.stringify({ data }), { headers: { "Content-Type": "application/json" } });
const user = { user_id: "user-1", email: "member@example.com", display_name: "Member", status: "active", has_password: false };
afterEach(() => vi.restoreAllMocks());
function Location() { const location = useLocation(); return <output>{location.pathname}{location.search}{location.hash}</output>; }

it("uses recent server policy, offers permitted alternatives and explicitly confirms a scrubbed scoped email link", async () => {
  setLocale("en", { persist: false });
  let sessionAllowed = true;
  const verified = vi.fn();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/me")) return json(user);
    if (url.includes("/verification?")) {
      expect(new URL(url).searchParams.get("scope")).toBe("admin");
      return json({ scope: "admin", mode: sessionAllowed ? "session" : "email", verified: sessionAllowed, reauthenticated_until: null, allowed_methods: sessionAllowed ? [] : ["email", "sso", "password"], providers: [{ slug: "company", display_name: "Company SSO", label: {}, supports_reauthentication: false }] });
    }
    if (url.endsWith("/email-reauthentication/confirm")) {
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toEqual({ token: "email-proof", scope: "admin" });
      return json({ reauthenticated_until: "2030-01-01T00:05:00Z", return_to: "/settings?section=users" });
    }
    if (url.endsWith("/email-reauthentication/request")) {
      expect(JSON.parse(String(init?.body))).toEqual({ scope: "admin", return_to: "/settings?section=users" });
      return json({ status: "email_sent" });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  const { unmount } = render(<IdentityVerificationPanel scope="admin" returnTo="/settings?section=users" onVerified={verified} />);
  expect(await screen.findByText("Your recent sign-in allows this action.")).toBeInTheDocument();
  expect(verified).toHaveBeenCalledWith(null, undefined, "session");
  expect(fetchMock.mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
  unmount();

  sessionAllowed = false;
  const emailPanel = render(<IdentityVerificationPanel scope="admin" returnTo="/settings?section=users" />);
  expect(await screen.findByRole("button", { name: "Confirm with Company SSO" })).toBeInTheDocument();
  expect(screen.getByLabelText("Current password")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Send verification email" }));
  expect(await screen.findByText("A verification link was sent. Open it in this browser to continue.")).toBeInTheDocument();
  emailPanel.unmount();
  render(<MemoryRouter initialEntries={["/auth/verify?scope=admin#token=email-proof"]}>
    <IdentityVerificationScreen /><Location />
  </MemoryRouter>);
  const confirm = await screen.findByRole("button", { name: "Confirm identity" });
  await waitFor(() => expect(screen.getByText(/^\/auth\/verify/)).not.toHaveTextContent("email-proof"));
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/email-reauthentication/confirm"))).toBe(false);
  fireEvent.click(confirm);
  expect(await screen.findByText("/settings?section=users&sso_verified=1")).toBeInTheDocument();
});
