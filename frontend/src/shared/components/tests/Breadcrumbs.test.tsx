import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { Breadcrumbs } from "../Breadcrumbs";

function renderWithRouter(items: Array<{ label: string; href?: string }>) {
  return render(
    <MemoryRouter>
      <Breadcrumbs items={items} />
    </MemoryRouter>,
  );
}

describe("Breadcrumbs", () => {
  it("renders nothing when there is no trail to show", () => {
    // A single crumb with nowhere to go only repeats the heading below it, so
    // the whole nav is dropped rather than rendered empty.
    const { container } = renderWithRouter([]);
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for a lone crumb with no destination", () => {
    renderWithRouter([{ label: "Account" }]);
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
  });

  it("renders intermediate items as links and the last as aria-current page", () => {
    renderWithRouter([
      { label: "Library", href: "/library" },
      { label: "Folder", href: "/library/folder" },
      { label: "Note" },
    ]);

    const libraryLink = screen.getByRole("link", { name: "Library" });
    expect(libraryLink).toHaveAttribute("href", "/library");
    const folderLink = screen.getByRole("link", { name: "Folder" });
    expect(folderLink).toHaveAttribute("href", "/library/folder");

    const current = screen.getByText("Note");
    expect(current).toHaveAttribute("aria-current", "page");
    expect(current.tagName).toBe("SPAN");
  });

  it("renders the last item as a plain span even when an href is provided", () => {
    renderWithRouter([
      { label: "Library", href: "/library" },
      { label: "Note", href: "/library/note" },
    ]);

    const current = screen.getByText("Note");
    expect(current).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("link", { name: "Note" })).not.toBeInTheDocument();
  });

  it("exposes full crumb text via the title attribute on truncating labels", () => {
    renderWithRouter([
      { label: "A very long parent folder title", href: "/library" },
      { label: "Current page with a long name" },
    ]);

    const link = screen.getByRole("link", { name: "A very long parent folder title" });
    expect(link).toHaveClass("breadcrumb-label");
    expect(link).toHaveAttribute("title", "A very long parent folder title");

    const current = screen.getByText("Current page with a long name");
    expect(current).toHaveClass("breadcrumb-label");
    expect(current).toHaveAttribute("title", "Current page with a long name");
  });

  it("renders separators between items but not before the first", () => {
    const { container } = renderWithRouter([
      { label: "A", href: "/a" },
      { label: "B", href: "/b" },
      { label: "C" },
    ]);

    // ChevronRight icons are decorative svgs with aria-hidden
    const separators = container.querySelectorAll('svg[aria-hidden="true"]');
    expect(separators).toHaveLength(2);
  });
});
