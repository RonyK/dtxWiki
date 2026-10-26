import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../shared/auth/AuthContext";
import { AuthScreen, safeNextPath } from "../AuthScreen";
import { LoginMethodsSection } from "../LoginMethodsSection";

const provider = {
  slug: "example", method_id: "example-oidc", protocol: "oidc", display_name: "Example",
  label: { ko: "Example로 계속", en: "Continue with Example" }, icon_url: null,
  background: "#0055aa", foreground: "#ffffff",
  start_url: "/api/v1/auth/sso/example/start", supports_reauthentication: true,
};
const identity = { id: "identity-1", provider_slug: "example", last_email: "profile@example.com", last_login_at: null, can_unlink: false };
const user = { user_id: "user-1", email: "external@example.com", display_name: "External User", status: "active", has_password: false, session_method: "ext", session_provider_slug: "example" };
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
afterEach(() => vi.restoreAllMocks());

function Location() { const location = useLocation(); return <output>{location.pathname}{location.search}</output>; }

it("uses capability-driven external login, safe next, remember policy and fixed callback errors", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => String(input).endsWith("/capabilities")
    ? json({ data: { signup_enabled: false, password_login: { enabled: false }, login_methods: [
      { ...provider, label: { en: "Continue" } },
      { ...provider, slug: "company", display_name: "Company SSO", label: { en: "Continue" } },
      { ...provider, slug: "localized", display_name: "", label: { en: "Localized provider" } },
    ], password_recovery: { mode: "email", admin_assisted: false } } })
    : json({ error_code: "invalid_session", message: "Missing cookie", request_id: "", warnings: [] }, 401));
  render(<MemoryRouter initialEntries={["/login?auth_error=account_link_required&next=%2Foauth%2Fauthorize%3Fstate%3Done"]}>
    <AuthProvider loadInstanceCapabilities><AuthScreen /><Location /></AuthProvider>
  </MemoryRouter>);
  const button = await screen.findByRole("link", { name: "Example" });
  expect(screen.getByRole("link", { name: "Company SSO" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Localized provider" })).toBeInTheDocument();
  expect(button).toHaveStyle({ backgroundColor: "#0055aa", color: "#ffffff" });
  expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Forgot your password?" })).not.toBeInTheDocument();
  expect(button).toHaveAttribute("href", "/api/v1/auth/sso/example/start?next=%2Foauth%2Fauthorize%3Fstate%3Done&remember_me=true");
  fireEvent.click(screen.getByRole("checkbox", { name: "Keep me signed in on this browser" }));
  expect(button.getAttribute("href")).toContain("remember_me=false");
  expect(await screen.findByText("An existing account uses this email. Sign in using that account’s existing login method, then link this provider under Account settings → Add login method.")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText(/^\/login\?/)).not.toHaveTextContent("auth_error"));
  for (const unsafe of ["//evil.example", "/\\evil.example", "/%2f/evil.example", "/%252f/evil.example", "/a/../api/v1/auth/me", "/\u0001bad"]) {
    expect(safeNextPath(unsafe)).toBe("/dashboard");
  }
});

it("protects the final external method and refreshes methods after setting a password and unlinking", async () => {
  let accountEmail: string | null = null;
  let passwordLoginEnabled = true;
  let hasPassword = false;
  let linked = true;
  let proofAvailable = false;
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/me")) return json({ data: { ...user, email: accountEmail, has_password: hasPassword } });
    if (url.endsWith("/capabilities")) return json({ data: { signup_enabled: false, password_login: { enabled: passwordLoginEnabled, email_enrollment_enabled: true }, password_recovery: { mode: "email", admin_assisted: false } } });
    if (url.endsWith("/email-verification/request")) {
      expect(JSON.parse(String(init?.body))).toEqual({ email: user.email });
      return json({ data: { requested: true, verified: false } });
    }
    if (url.endsWith("/identities")) return json({ data: { has_password: hasPassword, identities: linked ? [{ ...identity, can_unlink: hasPassword }] : [], available_providers: [provider] } });
    if (url.includes("/verification?")) return json({ data: { scope: "account", mode: "sso", verified: proofAvailable, reauthenticated_until: null, allowed_methods: ["sso"], providers: [provider] } });
    if (url.endsWith("/password/set")) {
      expect(JSON.parse(String(init?.body))).toEqual({ new_password: "new-password-123" });
      if (!proofAvailable) return json({ error_code: "recent_reauthentication_required", message: "Recent verification required", request_id: "", warnings: [] }, 401);
      hasPassword = true;
      return json({ data: { user: { ...user, has_password: true }, expires_at: "2030-01-01T00:00:00Z" } });
    }
    if (url.endsWith("/identities/identity-1") && init?.method === "DELETE") {
      linked = false;
      return json({ data: { user: { ...user, has_password: true }, expires_at: "2030-01-01T00:00:00Z" } });
    }
    throw new Error(`Unexpected URL ${url}`);
  });
  const enrollment = render(<AuthProvider><LoginMethodsSection /></AuthProvider>);
  expect(await screen.findByRole("button", { name: "Unlink" })).toBeDisabled();
  expect(screen.getByRole("link", { name: "Add login method" })).toHaveAttribute("href", "#login-methods-add");
  expect(screen.getByText("All sign-in providers available on this server are already connected.")).toBeInTheDocument();
  expect(screen.getByText("IdP profile email: profile@example.com")).toBeInTheDocument();
  expect(screen.getByText("A provider profile email may differ from your account email.")).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Verify with Example" })).toBeInTheDocument();
  expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  fireEvent.change(await screen.findByLabelText("Account email"), { target: { value: user.email } });
  fireEvent.click(screen.getByRole("button", { name: "Add account email" }));
  expect(await screen.findByText("A confirmation link was sent. Open it in this browser to register your account email.")).toBeInTheDocument();
  expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  // A separate explicit email confirmation updates /me. Sending mail alone
  // must not expose the local-password form or replace the SSO identity.
  enrollment.unmount();
  accountEmail = user.email;
  passwordLoginEnabled = false;
  const ssoOnly = render(<AuthProvider><LoginMethodsSection /></AuthProvider>);
  await screen.findByText("All sign-in providers available on this server are already connected.");
  expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  ssoOnly.unmount();
  passwordLoginEnabled = true;
  render(<AuthProvider><LoginMethodsSection /></AuthProvider>);
  await screen.findByLabelText("New password");
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-password-123" } });
  fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "new-password-123" } });
  fireEvent.click(screen.getByRole("button", { name: "Set password" }));
  expect(await screen.findByText("Verify your identity again to continue.")).toBeInTheDocument();
  expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
  proofAvailable = true;
  fireEvent.click(screen.getByRole("button", { name: "Set password" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Unlink" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Unlink" }));
  expect(screen.getByRole("dialog", { name: "Unlink Example?" })).toBeInTheDocument();
  expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith("/identities/identity-1") && init?.method === "DELETE")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Confirm unlink" }));
  expect(await screen.findByRole("button", { name: "Link Example" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("link", { name: "Add login method" })).toHaveFocus());
  expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith("/identities/identity-1") && init?.method === "DELETE")).toBe(true);
});
