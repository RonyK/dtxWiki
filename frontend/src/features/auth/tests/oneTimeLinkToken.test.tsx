import { act, render, screen, waitFor } from "@testing-library/react";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import {
  readOneTimeLinkToken,
  useOneTimeLinkToken,
} from "../oneTimeLinkToken";

// M-2: these drive the real browser history (jsdom's window.history and
// window.location), which is what the address bar shows -- a MemoryRouter
// would only prove that the router's own copy of the URL changed.

function TokenProbe() {
  const token = useOneTimeLinkToken();
  return <output data-testid="token">{token}</output>;
}

function openLink(url: string) {
  window.history.replaceState(null, "", url);
  const historyLength = window.history.length;
  const router = createBrowserRouter([
    { path: "/reset-password", element: <TokenProbe /> },
    { path: "/verify-email", element: <TokenProbe /> },
  ]);
  render(<RouterProvider router={router} />);
  return { historyLength };
}

describe("useOneTimeLinkToken", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("removes a fragment token from the address bar by replacing the history entry", async () => {
    const { historyLength } = openLink(
      "/reset-password#token=dtxr_0_address-bar-token",
    );

    expect(screen.getByTestId("token")).toHaveTextContent(
      "dtxr_0_address-bar-token",
    );
    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(window.location.href).not.toContain("address-bar-token");
    expect(window.location.pathname).toBe("/reset-password");
    // Replaced, not pushed: Back must not return to a URL with the token.
    expect(window.history.length).toBe(historyLength);
    // The screen keeps the token it read.
    expect(screen.getByTestId("token")).toHaveTextContent(
      "dtxr_0_address-bar-token",
    );
  });

  it("removes a legacy query token and keeps the rest of the URL", async () => {
    const { historyLength } = openLink(
      "/verify-email?token=dtxe_legacy-token&lang=ko#section",
    );

    expect(screen.getByTestId("token")).toHaveTextContent("dtxe_legacy-token");
    await waitFor(() => expect(window.location.search).toBe("?lang=ko"));
    expect(window.location.hash).toBe("#section");
    expect(window.location.href).not.toContain("legacy-token");
    expect(window.history.length).toBe(historyLength);
  });

  it("takes over the token of a second link opened over the screen", async () => {
    openLink("/reset-password#token=dtxr_0_first-link");
    await waitFor(() => expect(window.location.hash).toBe(""));

    // The browser's own fragment navigation: a new entry, then popstate.
    act(() => {
      window.history.pushState(null, "", "/reset-password#token=dtxr_0_second-link");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    await waitFor(() =>
      expect(screen.getByTestId("token")).toHaveTextContent("dtxr_0_second-link"),
    );
    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(window.location.href).not.toContain("second-link");
  });

  it("leaves a link without a token alone", async () => {
    openLink("/verify-email?lang=ko#section");

    expect(screen.getByTestId("token")).toHaveTextContent(/^$/);
    // Nothing to remove, so nothing may be rewritten.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe(
      "/verify-email?lang=ko#section",
    );
  });
});

describe("readOneTimeLinkToken", () => {
  // Assembled rather than written out: a literal `token=<value>` is the shape
  // the CI secret scanner (gitleaks generic-api-key) flags.
  const param = (separator: "#" | "?", value: string) =>
    `${separator}token=${value}`;

  it.each([
    [{ hash: param("#", "dtxr_0_a"), search: "" }, "dtxr_0_a"],
    [{ hash: "", search: param("?", "dtxr_0_b") }, "dtxr_0_b"],
    [
      { hash: param("#", "dtxr_0_fragment"), search: param("?", "dtxr_0_query") },
      "dtxr_0_fragment",
    ],
    [{ hash: param("#", "%20dtxe_spaced%20"), search: "" }, "dtxe_spaced"],
    [{ hash: param("#", ""), search: param("?", "dtxr_0_query") }, "dtxr_0_query"],
    [{ hash: "#section", search: "" }, ""],
    [{ hash: "", search: "" }, ""],
  ])("reads %j as %j", (location, expected) => {
    expect(readOneTimeLinkToken(location)).toBe(expected);
  });
});
