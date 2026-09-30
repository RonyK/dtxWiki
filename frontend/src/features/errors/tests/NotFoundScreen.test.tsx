import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { i18n } from "../../../shared/i18n";
import { NotFoundScreen } from "../NotFoundScreen";

describe("NotFoundScreen", () => {
  beforeEach(async () => {
    // Other suites may flip the shared i18n singleton to "ko"; assert the
    // English copy under a known locale.
    await i18n.changeLanguage("en");
  });

  it("renders the 404 heading and a Back to dashboard link", () => {
    render(
      <MemoryRouter>
        <NotFoundScreen />
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("heading", { name: "We couldn't find that page" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Back to dashboard" }),
    ).toHaveAttribute("href", "/dashboard");
    expect(screen.getByRole("link", { name: "Open Context" })).toHaveAttribute(
      "href",
      "/context",
    );
  });
});
