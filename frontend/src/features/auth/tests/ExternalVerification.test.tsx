import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../shared/auth/AuthContext";
import { setLocale } from "../../../shared/i18n";
import { ExternalSignupScreen } from "../ExternalSignupScreen";
import { IdentityVerificationPanel } from "../IdentityVerificationPanel";
import { IdentityVerificationScreen } from "../IdentityVerificationScreen";

const json = (data: unknown) => new Response(JSON.stringify({ data }), { headers: { "Content-Type": "application/json" } });
const user = { user_id: "user-1", email: "member@example.com", display_name: "Member", status: "active", has_password: false };
afterEach(() => vi.restoreAllMocks());
function Location() { const location = useLocation(); return <output>{location.pathname}{location.search}{location.hash}</output>; }

it("uses the server's admin session policy and explicitly confirms a scoped email link after scrubbing it", async () => {
  setLocale("en", { persist: false });
  let sessionAllowed = true;
  const verified = vi.fn();
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/me")) return json(user);
    if (url.includes("/verification?")) {
      expect(new URL(url).searchParams.get("scope")).toBe("admin");
      return json({ scope: "admin", mode: sessionAllowed ? "session" : "email", verified: sessionAllowed, reauthenticated_until: null, providers: [] });
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
  expect(await screen.findByText("Your current SSO session allows this action.")).toBeInTheDocument();
  expect(verified).toHaveBeenCalledWith(null, undefined, "session");
  expect(fetchMock.mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
  unmount();

  sessionAllowed = false;
  const emailPanel = render(<IdentityVerificationPanel scope="admin" returnTo="/settings?section=users" />);
  fireEvent.click(await screen.findByRole("button", { name: "Send verification email" }));
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

it("requests a pending-signup email before account creation and consumes its fragment only after confirmation", async () => {
  setLocale("en", { persist: false });
  let confirmationLink = false;
  let mailConfigured = true;
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/me")) return json(user);
    if (url.endsWith("/pending-signups")) return json({ email: confirmationLink ? user.email : null, email_confirmation_required: mailConfigured, requested: confirmationLink });
    if (url.endsWith("/pending-signups/email")) {
      expect(JSON.parse(String(init?.body))).toEqual({ email: user.email });
      return json(mailConfigured
        ? { status: "email_sent", session: null, return_to: "/dashboard" }
        : { status: "complete", session: { user: { ...user, email_verified_at: null }, expires_at: "2030-01-01T00:00:00Z" }, return_to: "/dashboard" });
    }
    if (url.endsWith("/pending-signups/confirm")) {
      expect(JSON.parse(String(init?.body))).toEqual({ token: "signup-proof" });
      return json({ session: { user, expires_at: "2030-01-01T00:00:00Z" }, return_to: "/dashboard" });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  function Screen() {
    return <AuthProvider><Routes><Route path="/auth/signup/verify" element={<ExternalSignupScreen />} /><Route path="/dashboard" element={<p>Signed-in dashboard</p>} /></Routes><Location /></AuthProvider>;
  }
  const first = render(<MemoryRouter initialEntries={["/auth/signup/verify"]}><Screen /></MemoryRouter>);
  fireEvent.change(await screen.findByLabelText("Email"), { target: { value: user.email } });
  fireEvent.click(screen.getByRole("button", { name: "Send confirmation email" }));
  expect(await screen.findByText("A confirmation link was sent. Open it in this browser to finish creating your account.")).toBeInTheDocument();
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/pending-signups/confirm"))).toBe(false);
  first.unmount();

  confirmationLink = true;
  const confirmation = render(<MemoryRouter initialEntries={["/auth/signup/verify#token=signup-proof"]}><Screen /></MemoryRouter>);
  const confirm = await screen.findByRole("button", { name: "Confirm email and sign in" });
  await waitFor(() => expect(screen.getByText("/auth/signup/verify")).not.toHaveTextContent("signup-proof"));
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/pending-signups/confirm"))).toBe(false);
  fireEvent.click(confirm);
  expect(await screen.findByText("Signed-in dashboard")).toBeInTheDocument();
  confirmation.unmount();

  confirmationLink = false;
  mailConfigured = false;
  render(<MemoryRouter initialEntries={["/auth/signup/verify"]}><Screen /></MemoryRouter>);
  fireEvent.change(await screen.findByLabelText("Email"), { target: { value: user.email } });
  fireEvent.click(screen.getByRole("button", { name: "Complete registration" }));
  expect(await screen.findByText("Signed-in dashboard")).toBeInTheDocument();
  expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/pending-signups/confirm"))).toHaveLength(1);
});
