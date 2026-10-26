import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MarkdownView } from "../MarkdownView";

const mermaidMock = vi.hoisted(() => ({
  initialize: vi.fn(),
  parse: vi.fn(),
  render: vi.fn(),
}));

vi.mock("mermaid", () => ({ default: mermaidMock }));

describe("MarkdownView", () => {
  beforeEach(() => {
    mermaidMock.initialize.mockClear();
    // Default: a valid diagram (truthy ParseResult) so the render path runs;
    // invalid-syntax tests override with mockResolvedValueOnce(false).
    mermaidMock.parse.mockReset();
    mermaidMock.parse.mockResolvedValue({ diagramType: "flowchart" });
    mermaidMock.render.mockReset();
    mermaidMock.render.mockResolvedValue({
      svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80"><text>Graph label</text></svg>',
    });
  });

  it("projects an exact context media reference through the injected resolver", () => {
    const assetId = "123e4567-e89b-12d3-a456-426614174000";
    const body = `![Architecture](dtx-media://asset/${assetId})`;
    const resolveContextMediaAsset = vi.fn(
      (id: string) => ({
        status: "ready" as const,
        url: `/api/v1/pages/page-1/versions/version-2/media/${id}/content`,
      }),
    );

    render(
      <MemoryRouter>
        <MarkdownView
          body={body}
          resolveContextMediaAsset={resolveContextMediaAsset}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("img", { name: "Architecture" })).toHaveAttribute(
      "src",
      `/api/v1/pages/page-1/versions/version-2/media/${assetId}/content`,
    );
    expect(resolveContextMediaAsset).toHaveBeenCalledWith(assetId);
    expect(screen.getByRole("img", { name: "Architecture" })).toHaveAttribute(
      "data-dtx-media-state",
      "ready",
    );
    expect(body).toBe(`![Architecture](dtx-media://asset/${assetId})`);
  });

  it("keeps a managed unavailable reason through sanitization", () => {
    const assetId = "123e4567-e89b-12d3-a456-426614174000";
    const resolveContextMediaAsset = vi.fn(() => ({
      status: "unavailable" as const,
      reason: "version_unavailable" as const,
    }));

    render(
      <MemoryRouter>
        <MarkdownView
          body={`![Archived architecture](dtx-media://asset/${assetId})`}
          resolveContextMediaAsset={resolveContextMediaAsset}
        />
      </MemoryRouter>,
    );

    const placeholder = screen.getByRole("status");
    expect(placeholder).toHaveAttribute("data-dtx-media-state", "unavailable");
    expect(placeholder).toHaveAttribute(
      "data-dtx-media-reason",
      "version_unavailable",
    );
    expect(placeholder).toHaveTextContent("Archived architecture");
    expect(screen.queryByRole("img", { name: "Archived architecture" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry loading/ }))
      .not.toBeInTheDocument();
    expect(resolveContextMediaAsset).toHaveBeenCalledWith(assetId);
  });

  it("marks managed media unavailable when no resolver is supplied", () => {
    const assetId = "123e4567-e89b-12d3-a456-426614174000";

    render(
      <MemoryRouter>
        <MarkdownView body={`![Unresolved media](dtx-media://asset/${assetId})`} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("status")).toHaveAttribute(
      "data-dtx-media-reason",
      "resolver_unavailable",
    );
  });

  it("renders images responsively and opens an accessible exact-source viewer", async () => {
    const user = userEvent.setup();
    const source = "/api/v1/pages/page-1/versions/version-2/media/asset-1/content";

    render(
      <MemoryRouter>
        <MarkdownView body={`![Architecture](${source})`} />
      </MemoryRouter>,
    );

    const image = screen.getByRole("img", { name: "Architecture" });
    expect(image).toHaveClass("markdown-image");
    expect(image).toHaveAttribute("src", source);
    const trigger = screen.getByRole("button", { name: "Expand Architecture" });

    await user.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "Architecture viewer" });
    expect(dialog).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "Architecture" })[1]).toHaveAttribute(
      "src",
      source,
    );

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Architecture viewer" }))
      .not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("uses privacy-safe loading and a bounded manual failure recovery", async () => {
    const user = userEvent.setup();
    const source = "https://images.example.test/architecture.png";

    render(
      <MemoryRouter>
        <MarkdownView body={`![Remote architecture](${source})`} />
      </MemoryRouter>,
    );

    let image = screen.getByRole("img", { name: "Remote architecture" });
    expect(image).toHaveAttribute("loading", "lazy");
    expect(image).toHaveAttribute("decoding", "async");
    expect(image).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(image).not.toHaveAttribute("crossorigin");
    expect(image).toHaveAttribute("data-load-state", "loading");

    fireEvent.load(image);
    expect(image).toHaveAttribute("data-load-state", "loaded");

    fireEvent.error(image);

    let placeholder = screen.getByRole("status");
    expect(placeholder).toHaveClass("markdown-image-failure");
    expect(placeholder).toHaveAttribute("data-load-state", "failed");
    expect(placeholder).toHaveTextContent("This image could not be displayed.");
    expect(placeholder).toHaveTextContent("Alt text: Remote architecture");
    expect(placeholder).toHaveTextContent("Source: images.example.test");
    const original = screen.getByRole("link", {
      name: "Open original image: Remote architecture",
    });
    expect(original).toHaveAttribute("href", source);
    expect(original).toHaveAttribute("target", "_blank");
    expect(original).toHaveAttribute("rel", "noopener noreferrer");
    expect(original).toHaveAttribute("referrerpolicy", "no-referrer");

    await user.click(screen.getByRole("button", {
      name: "Retry loading Remote architecture",
    }));
    image = screen.getByRole("img", { name: "Remote architecture" });
    fireEvent.error(image);
    await user.click(screen.getByRole("button", {
      name: "Retry loading Remote architecture",
    }));
    image = screen.getByRole("img", { name: "Remote architecture" });
    fireEvent.error(image);

    placeholder = screen.getByRole("status");
    expect(placeholder).toHaveTextContent("Retry limit reached.");
    expect(screen.queryByRole("button", {
      name: "Retry loading Remote architecture",
    })).not.toBeInTheDocument();
    expect(screen.queryAllByRole("img", { name: "Remote architecture" }))
      .toHaveLength(0);
  });

  it("keeps relative and HTTPS images while stripping unsafe page image schemes", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            "![HTTPS image](https://images.example.test/safe.png)",
            "![Uppercase HTTPS image](HTTPS://images.example.test/case.png)",
            "![Relative image](/api/v1/pages/page-1/media/safe)",
            "![Scheme-relative image](//images.example.test/unsafe.png)",
            "![HTTP image](http://images.example.test/unsafe.png)",
            "![Script image](javascript:alert(1))",
            "![Blob image](blob:https://dtxwiki.example/asset-id)",
            "![Data image](data:image/png;base64,unsafe)",
          ].join("\n\n")}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("img", { name: "HTTPS image" })).toHaveAttribute(
      "src",
      "https://images.example.test/safe.png",
    );
    expect(screen.getByRole("img", { name: "Relative image" })).toHaveAttribute(
      "src",
      "/api/v1/pages/page-1/media/safe",
    );
    expect(
      screen.getByRole("img", { name: "Uppercase HTTPS image" }),
    ).toHaveAttribute("src", "https://images.example.test/case.png");
    for (const name of [
      "Scheme-relative image",
      "HTTP image",
      "Script image",
      "Blob image",
      "Data image",
    ]) {
      expect(screen.queryByRole("img", { name })).not.toBeInTheDocument();
      expect(screen.getByText(`Alt text: ${name}`)).toBeInTheDocument();
    }
  });

  it("keeps a linked image as one native link without nesting the viewer button", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={"[![Architecture](https://example.com/architecture.png)](https://example.com/details)"}
        />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: "Architecture" });
    const image = screen.getByRole("img", { name: "Architecture" });
    const clickStates: Array<{
      ctrlKey: boolean;
      defaultPrevented: boolean;
      metaKey: boolean;
    }> = [];
    const contextMenuStates: boolean[] = [];
    link.addEventListener("click", (event) => {
      clickStates.push({
        ctrlKey: event.ctrlKey,
        defaultPrevented: event.defaultPrevented,
        metaKey: event.metaKey,
      });
      event.preventDefault();
    });
    link.addEventListener("contextmenu", (event) => {
      contextMenuStates.push(event.defaultPrevented);
      event.preventDefault();
    });

    expect(link).toHaveAttribute("href", "https://example.com/details");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toContainElement(image);
    expect(image).toHaveAttribute("src", "https://example.com/architecture.png");
    expect(link.querySelector("button")).toBeNull();
    expect(screen.queryByRole("button", { name: "Expand Architecture" }))
      .not.toBeInTheDocument();

    fireEvent.click(image);
    fireEvent.click(image, { ctrlKey: true });
    fireEvent.click(image, { metaKey: true });
    fireEvent.contextMenu(image);

    expect(clickStates).toEqual([
      { ctrlKey: false, defaultPrevented: false, metaKey: false },
      { ctrlKey: true, defaultPrevented: false, metaKey: false },
      { ctrlKey: false, defaultPrevented: false, metaKey: true },
    ]);
    expect(contextMenuStates).toEqual([false]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.error(image);

    const failedLink = document.querySelector<HTMLAnchorElement>(
      'a[href="https://example.com/details"]',
    );
    expect(failedLink).not.toBeNull();
    expect(failedLink).toHaveAttribute(
      "href",
      "https://example.com/details",
    );
    expect(failedLink).toHaveAccessibleName(/Architecture/);
    expect(failedLink!.querySelector(".markdown-image-failure")).not.toBeNull();
    const linkedActions = failedLink!.nextElementSibling as HTMLElement;
    expect(linkedActions).toHaveClass("markdown-linked-image-actions");
    const retry = screen.getByRole("button", {
      name: "Retry loading Architecture",
    });
    const openOriginal = screen.getByRole("link", {
      name: "Open original image: Architecture",
    });
    expect(linkedActions).toContainElement(retry);
    expect(linkedActions).toContainElement(openOriginal);
    expect(openOriginal).toHaveAttribute(
      "href",
      "https://example.com/architecture.png",
    );
    expect(document.querySelectorAll("a")).toHaveLength(2);
    expect(document.querySelector("a a, a button")).toBeNull();

    fireEvent.click(retry);

    expect(screen.getByRole("img", { name: "Architecture" })).toBeInTheDocument();
    expect(linkedActions).toBeEmptyDOMElement();
  });

  it.each([
    {
      body: "[Details ![Mixed diagram](https://images.example.test/mixed.png)](https://example.com/mixed-details)",
      href: "https://example.com/mixed-details",
      label: "mixed text and image",
      names: ["Mixed diagram"],
    },
    {
      body: "[![First diagram](https://images.example.test/first.png) ![Second diagram](https://images.example.test/second.png)](https://example.com/multiple-details)",
      href: "https://example.com/multiple-details",
      label: "multiple images",
      names: ["First diagram", "Second diagram"],
    },
    {
      body: "[*![Emphasized diagram](https://images.example.test/emphasized.png)*](https://example.com/emphasized-details)",
      href: "https://example.com/emphasized-details",
      label: "an emphasized image",
      names: ["Emphasized diagram"],
    },
  ])("keeps $label passive within the outer link, including failures", ({
    body,
    href,
    names,
  }) => {
    render(
      <MemoryRouter>
        <MarkdownView body={body} />
      </MemoryRouter>,
    );

    const link = document.querySelector<HTMLAnchorElement>(
      `a[href=${JSON.stringify(href)}]`,
    );
    expect(link).not.toBeNull();
    const images = names.map((name) => screen.getByRole("img", { name }));
    for (const image of images) {
      expect(link).toContainElement(image);
      expect(image.closest("button")).toBeNull();
    }
    expect(document.querySelector("a button, a a")).toBeNull();

    for (const image of images) {
      fireEvent.error(image);
    }

    expect(link!.querySelectorAll(".markdown-image-failure")).toHaveLength(
      names.length,
    );
    expect(link!.querySelector(".markdown-image-failure-actions")).toBeNull();
    const linkedActions = link!.nextElementSibling as HTMLElement;
    expect(linkedActions).toHaveClass("markdown-linked-image-actions");
    expect(
      linkedActions.querySelectorAll(".markdown-image-failure-actions"),
    ).toHaveLength(names.length);
    for (const name of names) {
      expect(
        within(linkedActions).getByRole("button", {
          name: `Retry loading ${name}`,
        }),
      ).toBeInTheDocument();
      expect(
        within(linkedActions).getByRole("link", {
          name: `Open original image: ${name}`,
        }),
      ).toBeInTheDocument();
    }
    expect(document.querySelectorAll("a")).toHaveLength(1 + names.length);
    expect(document.querySelector("a button, a a")).toBeNull();
  });

  it("gives an empty-alt linked image a localized link name", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={"[![](https://example.com/decorative.png)](https://example.com/details)"}
        />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: "Open linked image" });
    expect(link.querySelector("img")).toHaveAttribute("alt", "");
    expect(link.querySelector("button")).toBeNull();

    fireEvent.error(link.querySelector("img")!);

    const failedLink = screen.getByRole("link", { name: "Open linked image" });
    expect(failedLink).toHaveAttribute("href", "https://example.com/details");
    expect(failedLink.querySelector(".markdown-image-failure")).not.toBeNull();
    const linkedActions = failedLink.nextElementSibling as HTMLElement;
    expect(linkedActions).toHaveClass("markdown-linked-image-actions");
    expect(
      within(linkedActions).getByRole("button", {
        name: "Retry loading Image",
      }),
    ).toBeInTheDocument();
    expect(
      within(linkedActions).getByRole("link", {
        name: "Open original image: Image",
      }),
    ).toBeInTheDocument();
    expect(document.querySelector("a a, a button")).toBeNull();
  });

  it("does not resolve a non-exact context media reference", () => {
    const assetId = "123e4567-e89b-12d3-a456-426614174000";
    const resolveContextMediaAsset = vi.fn(() => ({
      status: "ready" as const,
      url: "/api/v1/unexpected",
    }));

    render(
      <MemoryRouter>
        <MarkdownView
          body={`![Architecture](dtx-media://asset/${assetId}?download=1)`}
          resolveContextMediaAsset={resolveContextMediaAsset}
        />
      </MemoryRouter>,
    );

    expect(resolveContextMediaAsset).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Alt text: Architecture",
    );
    expect(screen.queryByRole("img", { name: "Architecture" }))
      .not.toBeInTheDocument();
  });

  it("strips page frontmatter before rendering Markdown body", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            "---",
            'title: "Release Notes"',
            "slug: release-notes",
            'icon: "lucide:file-text"',
            "type: overview",
            'tags: ["release"]',
            "---",
            "",
            "# Release Notes",
          ].join("\n")}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "Release Notes" })).toBeInTheDocument();
    expect(screen.queryByText(/lucide:file-text/)).not.toBeInTheDocument();
    expect(screen.queryByText(/type: overview/)).not.toBeInTheDocument();
  });

  it("does not strip ordinary markdown thematic breaks", () => {
    render(
      <MemoryRouter>
        <MarkdownView body={["---", "This is regular markdown.", "---"].join("\n")} />
      </MemoryRouter>,
    );

    expect(screen.getByText("This is regular markdown.")).toBeInTheDocument();
  });

  it("can hide a duplicate leading title heading without changing other headings", () => {
    render(
      <MemoryRouter>
        <MarkdownView body={"# Hello\n\n## Details"} stripLeadingHeading="Hello" />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("heading", { name: "Hello" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Details" })).toBeInTheDocument();
  });

  it("renders heading anchors, GFM alerts, and safe link behavior", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <MarkdownView
          workspaceId="11111111-1111-4111-8111-111111111111"
          linkTargets={[
            { locator: "/pages/release-notes", href: "/p/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222", target_page_id: "22222222-2222-4222-8222-222222222222", state: "ok" },
            { locator: "/context/missing.md", href: null, target_page_id: null, state: "unavailable" },
            { locator: "/context/missing-image.md", href: null, target_page_id: null, state: "unavailable" },
          ]}
          body={[
            "# Release Notes",
            "",
            "> [!NOTE]",
            "> Keep this cited.",
            "",
            "[Missing](/context/missing.md), [Short](/p/22222222-2222-4222-8222-222222222222#details), [Reference][target],",
            "",
            "[target]: /pages/release-notes",
            "",
            "[Internal](/pages/release-notes), [Hash](#user-content-release-notes), [External](https://example.com), and [Protocol](//example.com)",
            '[![Linked image](https://images.example.test/linked.png)](/context/missing-image.md)',
            '<div><a href="https://raw-html.example.test"data-dtx-unavailable-reference="dtx-markdown-reference-0" data-dtx-reference-wikilink="true" data-dtx-reference-token="dtx-markdown-reference-0" data-context-link-locator="/context/missing.md">Raw HTML link</a></div>',
          ].join("\n")}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "Release Notes" }))
      .toHaveAttribute("id", "user-content-release-notes");
    expect(screen.getByRole("link", { name: "Release Notes" }))
      .toHaveAttribute("href", "#user-content-release-notes");
    expect(screen.getByRole("link", { name: "Release Notes" }))
      .not.toHaveAttribute("target");
    expect(screen.getByText("Keep this cited.")).toBeInTheDocument();
    expect(screen.getByText("Keep this cited.").closest(".markdown-alert"))
      .toHaveClass("markdown-alert-note");
    expect(screen.getByRole("link", { name: "Internal" }))
      .toHaveAttribute("href", "/p/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222");
    expect(screen.getByRole("link", { name: "Internal" }))
      .not.toHaveAttribute("node");
    expect(screen.getByRole("link", { name: "Hash" }))
      .toHaveAttribute("href", "#user-content-release-notes");
    expect(screen.getByRole("link", { name: "Hash" })).not.toHaveAttribute("target");
    expect(screen.getByRole("link", { name: "External" }))
      .toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "External" }))
      .toHaveAttribute("rel", "noopener noreferrer");
    // Budget exception: legacy destination pins, short preview inheritance,
    // reference links, and unavailable navigation are distinct render paths.
    expect(screen.queryByRole("link", { name: "Protocol" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Missing" })).not.toBeInTheDocument();
    const missing = screen.getByRole("button", { name: "Missing" });
    expect(missing).toHaveAttribute("aria-haspopup", "dialog");
    expect(missing).toHaveAttribute("aria-expanded", "false");
    expect(missing).toHaveAttribute("aria-describedby");
    expect(missing).toHaveClass("unavailable-reference__trigger");
    await user.click(missing);
    const unavailableDialog = screen.getByRole("dialog");
    expect(unavailableDialog).toHaveTextContent("This item may be missing or unavailable to your current account.");
    expect(unavailableDialog).toHaveTextContent("/context/missing.md");
    expect(within(unavailableDialog).getByRole("button", { name: "Copy authored reference" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(missing).toHaveAttribute("aria-expanded", "false");
    expect(document.activeElement).toBe(missing);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.pointerEnter(missing, { pointerType: "mouse" });
    const hoverDialog = screen.getByRole("dialog");
    const referenceRoot = missing.closest(".unavailable-reference");
    expect(referenceRoot).not.toBeNull();
    fireEvent.pointerLeave(referenceRoot!);
    fireEvent.pointerEnter(hoverDialog, { pointerType: "mouse" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.click(within(hoverDialog).getByRole("button", { name: "Close explanation" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.pointerEnter(missing, { pointerType: "touch" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(missing);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close explanation" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Raw HTML link" })).toHaveAttribute("href", "https://raw-html.example.test");
    expect(screen.queryByRole("button", { name: "Raw HTML link" })).not.toBeInTheDocument();
    expect(document.querySelector('[data-dtx-reference-token="dtx-markdown-reference-0"]')).toBeNull();
    expect(screen.queryByRole("button", { name: "Raw HTML link" })).not.toBeInTheDocument();
    const linkedImage = screen.getByRole("img", { name: "Linked image" });
    fireEvent.error(linkedImage);
    const linkedImageFailure = screen.getByRole("status");
    const unavailableImageTrigger = linkedImageFailure.closest(".unavailable-reference__trigger");
    if (!unavailableImageTrigger) throw new Error("Unavailable linked image trigger is missing");
    expect(unavailableImageTrigger.querySelector("button, a")).toBeNull();
    expect(screen.getByRole("link", { name: "Open original image: Linked image" }))
      .not.toBe(unavailableImageTrigger);
    expect(screen.getByRole("link", { name: "Short" })).toHaveAttribute("href", "/p/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222#user-content-details");
    expect(screen.getByRole("link", { name: "Reference" })).toHaveAttribute("href", "/p/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222");
  });

  it("clobber-prefixes raw HTML ids so authored anchors cannot hijack the app namespace", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            '<h2 id="raw-anchor">Raw heading</h2>',
            "",
            "[Jump](#raw-anchor)",
          ].join("\n")}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "Raw heading" }))
      .toHaveAttribute("id", "user-content-raw-anchor");
    expect(document.getElementById("raw-anchor")).toBeNull();
    expect(screen.getByRole("link", { name: "Raw heading" }))
      .toHaveAttribute("href", "#user-content-raw-anchor");
    expect(screen.getByRole("link", { name: "Jump" }))
      .toHaveAttribute("href", "#user-content-raw-anchor");
  });

  it("keeps GFM footnote links and ids inside the prefixed anchor namespace", () => {
    render(
      <MemoryRouter>
        <MarkdownView body={"A claim.[^1]\n\n[^1]: Footnote text."} />
      </MemoryRouter>,
    );

    const ref = document.querySelector('a[href="#user-content-fn-1"]');
    expect(ref).toBeInTheDocument();
    expect(ref).toHaveAttribute("id", "user-content-fnref-1");
    expect(document.getElementById("user-content-fn-1"))
      .toHaveTextContent("Footnote text.");
    expect(document.querySelector('a[href="#user-content-fnref-1"]'))
      .toBeInTheDocument();
  });

  it("renders dtxWiki wikilinks and citation markers with safe metadata", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            "See [[release-notes|Release Notes]] and [[Missing Page]].",
            "",
            "Claim @cite{source-1#section-a}.",
          ].join("\n")}
        />
      </MemoryRouter>,
    );

    // P10.1: /pages/{slug} is gone — a wikilink the server did not resolve to
    // an actor-visible page id (here: no wikilinkTargets/wikilinkPages were
    // even supplied) can no longer navigate anywhere in-app and renders
    // broken instead of pointing at a dead route.
    const wikilink = screen.getByRole("button", { name: "Release Notes" });
    expect(wikilink).toHaveClass("unavailable-reference__trigger");
    expect(screen.getByRole("button", { name: "Missing Page" }))
      .toHaveClass("unavailable-reference__trigger");

    const citation = screen.getByText("@cite{source-1#section-a}");
    expect(citation.tagName).toBe("CITE");
    expect(citation).toHaveAttribute("data-source-id", "source-1");
    expect(citation).toHaveAttribute("data-source-anchor", "section-a");
    expect(citation).toHaveClass("cite-ref");
  });

  it("renders deleted Library citations as tombstone markers", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body="Claim @cite{source-1#section-a}."
          deletedSources={[
            {
              citationKey: "source-1",
              label: "API Source (deleted, v2)",
            },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.queryByText("@cite{source-1#section-a}")).not.toBeInTheDocument();
    const citation = screen.getByText("API Source (deleted, v2)");
    expect(citation.tagName).toBe("CITE");
    expect(citation).toHaveClass("cite-ref");
    expect(citation).toHaveClass("cite-ref-deleted");
    expect(citation).toHaveAttribute("data-source-id", "source-1");
    expect(citation).toHaveAttribute("data-source-anchor", "section-a");
    expect(citation).toHaveAttribute("data-source-deleted", "true");
  });

  it("resolves wikilinks through page aliases when no slug matches", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body="See [[Launch Plan]]."
          wikilinkPages={[
            {
              slug: "release-notes",
              title: "Release Notes",
              aliases: ["Launch Plan"],
            },
          ]}
        />
      </MemoryRouter>,
    );

    // P10.1: resolved only to a slug (no page id) — /pages/{slug} is gone, so
    // this renders broken rather than pointing at a dead route.
    expect(screen.getByRole("button", { name: "Launch Plan" }))
      .toHaveClass("unavailable-reference__trigger");
  });

  it("uses a stable canonical Context target after the target is renamed", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body="See [[Old Name]], [[Old Name#Breaking Changes|changes]], and [[link to nothing]]."
          workspaceId="workspace-1"
          wikilinkTargets={[
            {
              target_slug: "old-name",
              target_page_id: "target-page-id",
              current_slug: "new-name",
            },
            {
              target_slug: "link-to-nothing",
              target_page_id: null,
              current_slug: null,
            },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Old Name" }))
      .toHaveAttribute("href", "/p/workspace-1/target-page-id");
    expect(screen.getByRole("link", { name: "changes" }))
      .toHaveAttribute(
        "href",
        "/p/workspace-1/target-page-id#user-content-breaking-changes",
      );
    expect(screen.getByRole("button", { name: "link to nothing" }))
      .toHaveClass("unavailable-reference__trigger");
  });

  it("tokenizes wikilinks with non-ASCII text as broken (no page id resolved)", () => {
    // P10.1: /pages/{slug} is gone, so an unresolved cross-page reference
    // renders broken regardless of script — this keeps regression coverage on
    // the tokenizer regex matching Korean text inside [[...]].
    render(
      <MemoryRouter>
        <MarkdownView body="See [[한글 문서]]." />
      </MemoryRouter>,
    );

    expect(screen.getByRole("button", { name: "한글 문서" }))
      .toHaveClass("unavailable-reference__trigger");
  });
  it("renders a local heading anchor wikilink, and a cross-page one without a resolved id as broken", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body="See [[#Decision Log]] and [[Release Notes#Breaking Changes|changes]]."
          wikilinkPages={[
            {
              slug: "release-notes",
              title: "Release Notes",
              aliases: [],
            },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "#Decision Log" }))
      .toHaveAttribute("href", "#user-content-decision-log");
    // P10.1: a cross-page target resolved only to a slug (no page id) can no
    // longer route through the deleted /pages/{slug} URL — it must NOT
    // degrade into a same-document anchor jump (that would scroll the
    // CURRENT page instead of pointing at Release Notes), so it renders
    // broken instead.
    expect(screen.getByRole("button", { name: "changes" }))
      .toHaveClass("unavailable-reference__trigger");
  });

  it("builds the canonical Context permalink for a resolved wikilink target when a workspace id is supplied", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body="See [[Old Name]] and [[Old Name#Breaking Changes|changes]]."
          wikilinkTargets={[
            {
              target_slug: "old-name",
              target_page_id: "target-page-id",
              current_slug: "new-name",
            },
          ]}
          workspaceId="workspace-1"
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Old Name" })).toHaveAttribute(
      "href",
      "/p/workspace-1/target-page-id",
    );
    expect(screen.getByRole("link", { name: "changes" })).toHaveAttribute(
      "href",
      "/p/workspace-1/target-page-id#user-content-breaking-changes",
    );
  });

  it("renders highlights and inline tags while skipping code and links", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          workspaceId="11111111-1111-4111-8111-111111111111"
          body={[
            "Mark ==important claim== and tag #release-note.",
            "",
            "`#not-a-tag` and [link #also-not](https://example.com/page#anchor) stay plain.",
          ].join("\n")}
        />
      </MemoryRouter>,
    );

    const highlight = screen.getByText("important claim");
    expect(highlight.tagName).toBe("MARK");
    expect(highlight).toHaveClass("markdown-highlight");

    const tag = screen.getByRole("link", { name: "#release-note" });
    expect(tag).toHaveAttribute("href", "/workspaces/11111111-1111-4111-8111-111111111111/search?q=%23release-note");
    expect(tag).toHaveAttribute("data-tag", "release-note");
    expect(tag).toHaveClass("inline-tag");

    expect(screen.getByText("#not-a-tag").tagName).toBe("CODE");
    expect(screen.getByRole("link", { name: "link #also-not" }))
      .toHaveAttribute("href", "https://example.com/page#anchor");
    expect(document.querySelectorAll(".inline-tag")).toHaveLength(1);
  });

  it("allows sanitized video figures without preserving unsafe attributes", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            '<figure onclick="alert(1)">',
            '<video controls width="1600" height="900" src="https://example.com/demo.mp4" onerror="alert(1)" title="Demo"></video>',
            "<figcaption>Demo video</figcaption>",
            "</figure>",
            '<video controls src="javascript:alert(1)"></video>',
          ].join("\n")}
        />
      </MemoryRouter>,
    );

    const figure = document.querySelector("figure");
    const videos = document.querySelectorAll("video");

    expect(figure).toBeInTheDocument();
    expect(figure).not.toHaveAttribute("onclick");
    expect(screen.getByText("Demo video")).toBeInTheDocument();
    expect(videos[0]).toHaveAttribute("controls");
    expect(videos[0]).toHaveAttribute("src", "https://example.com/demo.mp4");
    expect(videos[0]).toHaveAttribute("title", "Demo");
    expect(videos[0]).toHaveClass("markdown-video");
    expect(videos[0]).not.toHaveAttribute("width");
    expect(videos[0]).not.toHaveAttribute("height");
    expect(videos[0]?.style.getPropertyValue("--markdown-video-width-cap"))
      .toBe("1600px");
    expect(videos[0]).not.toHaveAttribute("onerror");
    expect(videos[1]).not.toHaveAttribute("src");
  });

  it("renders every untrusted resource and link as non-navigable text", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            '<img alt="Raw beacon" src="https://beacon.example/raw.png">',
            '<video controls src="https://beacon.example/raw.mp4"></video>',
            "",
            "![Remote image](https://beacon.example/markdown.png)",
            "",
            "![Protocol-relative image](//beacon.example/markdown.png)",
            "",
            "![Inline image](data:image/svg+xml,unsafe)",
            "",
            "![Blob image](blob:https://dtxwiki.example/asset-id)",
            "",
            "![Relative image](/api/v1/library/relative.png)",
            "",
            `[Same-origin guide](/docs/guide)`,
            "",
            "[External documentation](https://docs.example/guide)",
            "",
            "[Unsafe link](javascript:alert(1))",
            "",
            "GFM autolink: https://autolink.example/guide",
          ].join("\n")}
          renderPolicy="untrusted-preview"
        />
      </MemoryRouter>,
    );

    expect(document.querySelector("img")).not.toBeInTheDocument();
    expect(document.querySelector("video")).not.toBeInTheDocument();
    expect(screen.getByText("Remote image")).toBeInTheDocument();
    expect(screen.getByText("Protocol-relative image")).toBeInTheDocument();
    expect(screen.getByText("Inline image")).toBeInTheDocument();
    expect(screen.getByText("Blob image")).toBeInTheDocument();
    expect(screen.getByText("Relative image")).toBeInTheDocument();
    expect(screen.getByText("Same-origin guide")).toBeInTheDocument();
    expect(screen.getByText("External documentation")).toBeInTheDocument();
    expect(screen.getByText("Unsafe link")).toBeInTheDocument();
    expect(screen.getByText(/^GFM autolink:/))
      .toHaveTextContent("GFM autolink: https://autolink.example/guide");
    expect(document.querySelector("a")).not.toBeInTheDocument();
  });

  it("renders untrusted Mermaid fences as literal code without active dispatch", () => {
    const payload = [
      "architecture-beta",
      "  group polluted(cloud)[Marker]",
      "  service a(server)[A] in __proto__",
    ].join("\n");

    render(
      <MemoryRouter>
        <MarkdownView
          body={`\`\`\`mermaid\n${payload}\n\`\`\``}
          renderPolicy="untrusted-preview"
        />
      </MemoryRouter>,
    );

    const pre = document.querySelector("pre");
    const code = pre?.querySelector(":scope > code");

    expect(pre?.children).toHaveLength(1);
    expect(code?.textContent).toBe(`${payload}\n`);
    expect(code?.childNodes).toHaveLength(1);
    expect(code?.firstChild?.nodeType).toBe(Node.TEXT_NODE);
    expect(document.querySelector(".markdown-code-block")).not.toBeInTheDocument();
    expect(document.querySelector(".mermaid-diagram")).not.toBeInTheDocument();
    expect(mermaidMock.initialize).not.toHaveBeenCalled();
    expect(mermaidMock.parse).not.toHaveBeenCalled();
    expect(mermaidMock.render).not.toHaveBeenCalled();
  });

  it("activates only server-confirmed internal links in untrusted answers", async () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={'[Confirmed](/pages/known) [External](https://example.test) ![Image](/x.png) <a href="/raw">Raw</a>'}
          linkTargets={[{
            locator: "/pages/known",
            href: "/p/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222",
            target_page_id: "22222222-2222-4222-8222-222222222222",
            state: "ok",
          }]}
          renderPolicy="confirmed-links"
          workspaceId="11111111-1111-4111-8111-111111111111"
        />
        <LocationStateProbe />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Confirmed" })).toHaveAttribute(
      "href",
      "/p/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222",
    );
    expect(document.querySelector('a[href="https://example.test"]')).not.toBeInTheDocument();
    expect(document.querySelector(".markdown-body")?.textContent).toContain("Image");
    expect(document.querySelector("img")).not.toBeInTheDocument();
    expect(document.querySelector('a[href="/raw"]')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Confirmed" }));
    expect(screen.getByTestId("markdown-location-state")).toHaveTextContent("null");
  });

  it("activates explicit typed workspace links only in opted-in editor previews", async () => {
    const sourceWorkspaceId = "11111111-1111-4111-8111-111111111111";
    const sourcePageId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const workspaceId = "33333333-3333-4333-8333-333333333333";
    const itemId = "22222222-2222-4222-8222-222222222222";
    const itemHref = `/workspaces/${workspaceId}/library/items/${itemId}?page=2`;
    const sourceHref = `/workspaces/${sourceWorkspaceId}/context/pages/${sourcePageId}`;
    const { rerender } = render(
      <MemoryRouter initialEntries={[{
        pathname: sourceHref,
        state: { previousNotice: "discard this" },
      }]}>
        <Routes>
          <Route
            path="*"
            element={(
              <MarkdownView
                allowWorkspaceNavigationLinks
                body={`[Library item](${itemHref}) [Spoofed target](${itemHref}&target=other) [Unknown route](/workspaces/${workspaceId}/admin)`}
                workspaceId={sourceWorkspaceId}
              />
            )}
          />
        </Routes>
        <LocationStateProbe />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: /Library item/ })).toHaveAttribute("href", itemHref);
    expect(screen.queryByRole("link", { name: "Spoofed target" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Spoofed target" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Unknown route" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unknown route" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: /Library item/ }));
    expect(screen.getByTestId("markdown-location-state")).toHaveTextContent(
      JSON.stringify({ dtxwikiReferenceReturnTo: sourceHref }),
    );

    rerender(
      <MemoryRouter>
        <MarkdownView
          allowWorkspaceNavigationLinks
          body={`[Library item](${itemHref})`}
          renderPolicy="confirmed-links"
          workspaceId={workspaceId}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("link", { name: "Library item" })).not.toBeInTheDocument();
    const askReference = screen.getByRole("button", { name: "Library item" });
    await userEvent.click(askReference);
    const askDialog = screen.getByRole("dialog");
    expect(askDialog).toHaveTextContent("This reference could not be confirmed.");
    expect(askDialog.querySelector("code")).toBeNull();
    expect(askDialog).not.toHaveTextContent(itemHref);
    expect(askDialog).not.toHaveTextContent(itemId);
    expect(within(askDialog).queryByRole("button", { name: "Copy authored reference" })).toBeNull();
    await userEvent.click(within(askDialog).getByRole("button", { name: "Close explanation" }));

    rerender(
      <MemoryRouter>
        <MarkdownView
          body={`[Library item](${itemHref})`}
          wikilinkMode="plain"
          workspaceId={sourceWorkspaceId}
        />
      </MemoryRouter>,
    );
    const publicReference = screen.getByRole("button", { name: "Library item" });
    await userEvent.click(publicReference);
    const publicDialog = screen.getByRole("dialog");
    expect(publicDialog).toHaveTextContent("This reference cannot be opened from this view.");
    expect(publicDialog.querySelector("code")).toBeNull();
    expect(publicDialog).not.toHaveTextContent(itemHref);
    expect(publicDialog).not.toHaveTextContent(itemId);
    expect(within(publicDialog).queryByRole("button", { name: "Copy authored reference" })).toBeNull();
  });

  it("prefixes fragments on confirmed internal links before sanitization", () => {
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const pageId = "22222222-2222-4222-8222-222222222222";
    const locator = "/pages/known#details";
    render(
      <MemoryRouter>
        <MarkdownView
          body={`[Confirmed](${locator})`}
          linkTargets={[{
            locator,
            href: `/p/${workspaceId}/${pageId}#details`,
            target_page_id: pageId,
            state: "ok",
          }]}
          renderPolicy="confirmed-links"
          workspaceId={workspaceId}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Confirmed" })).toHaveAttribute(
      "href",
      `/p/${workspaceId}/${pageId}#user-content-details`,
    );
  });

  it("keeps untrusted semantic payloads on the closed passive pipeline", () => {
    const assetId = "123e4567-e89b-12d3-a456-426614174000";
    const resolveContextMediaAsset = vi.fn(
      () => ({
        status: "ready" as const,
        url: `/api/v1/pages/page-1/versions/version-2/media/${assetId}/content`,
      }),
    );
    const tokenText =
      "[[Roadmap|Quarterly plan]] @cite{source-1} ==highlight== #tag";
    const mermaidPayload = "graph TD\n  A-->B";

    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            "# Passive heading",
            "",
            "Math stays literal: $E=mc^2$.",
            "",
            tokenText,
            "",
            "Comment anchor remains plain.",
            "",
            "> [!NOTE]",
            "> Passive alert text.",
            "",
            `![Context asset](dtx-media://asset/${assetId})`,
            "",
            "| Left | Right |",
            "|:--|--:|",
            "| a | b |",
            "",
            "```mermaid",
            mermaidPayload,
            "```",
          ].join("\n")}
          comments={[
            inlineComment(
              "c-untrusted",
              "exact",
              "Comment anchor remains plain.",
            ),
          ]}
          renderPolicy="untrusted-preview"
          resolveContextMediaAsset={resolveContextMediaAsset}
          wikilinkPages={[{ slug: "roadmap", title: "Roadmap" }]}
        />
      </MemoryRouter>,
    );

    const heading = screen.getByRole("heading", { name: "Passive heading" });
    expect(heading).not.toHaveAttribute("id");
    expect(heading.querySelector("a")).not.toBeInTheDocument();
    expect(screen.getByText("Math stays literal: $E=mc^2$.")).toBeInTheDocument();
    expect(document.querySelector(".katex")).not.toBeInTheDocument();
    expect(screen.getByText(tokenText)).toBeInTheDocument();
    expect(document.querySelector(".wikilink")).not.toBeInTheDocument();
    expect(document.querySelector("cite")).not.toBeInTheDocument();
    expect(document.querySelector("mark")).not.toBeInTheDocument();
    expect(document.querySelector("[data-comment-id]")).not.toBeInTheDocument();
    expect(document.querySelector("blockquote")).toHaveTextContent("[!NOTE]");
    expect(document.querySelector(".markdown-alert")).not.toBeInTheDocument();
    expect(resolveContextMediaAsset).not.toHaveBeenCalled();
    expect(document.querySelector("img")).not.toBeInTheDocument();
    expect(screen.getByText("Context asset")).toBeInTheDocument();
    const tableRegion = screen.getByRole("region", { name: "Table" });
    const tableHeadings = tableRegion.querySelectorAll("th");
    expect(tableHeadings[0]).toHaveStyle({ textAlign: "left" });
    expect(tableHeadings[1]).toHaveStyle({ textAlign: "right" });
    expect(document.querySelector("pre > code")?.textContent).toBe(
      `${mermaidPayload}\n`,
    );
    expect(mermaidMock.initialize).not.toHaveBeenCalled();
    expect(mermaidMock.parse).not.toHaveBeenCalled();
    expect(mermaidMock.render).not.toHaveBeenCalled();
  });

  it("renders highlighted code blocks with language and copy controls", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(
      <MemoryRouter>
        <MarkdownView
          body={"```ts\nconst answer: number = 42;\n```"}
          sourceLineNumbers
        />
      </MemoryRouter>,
    );

    const codeBlock = document.querySelector(".markdown-code-block");
    expect(codeBlock).toHaveAttribute("data-source-line", "1");
    expect(codeBlock).toHaveAttribute("data-source-end-line", "3");
    expect(codeBlock).toHaveAttribute("data-scroll-sync-anchor", "true");
    expect(codeBlock?.querySelector("pre")).not.toHaveAttribute(
      "data-scroll-sync-anchor",
    );
    expect(screen.getByText("TypeScript")).toBeInTheDocument();
    expect(document.querySelector("code.hljs")).toBeInTheDocument();
    expect(document.querySelector("pre")).toHaveAttribute("tabindex", "0");
    expect(document.querySelector("pre")).toHaveAttribute("role", "region");
    expect(document.querySelector("pre")).toHaveAttribute(
      "aria-label",
      "Scrollable TypeScript code",
    );

    await userEvent.click(screen.getByRole("button", { name: "Copy code" }));

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText.mock.calls[0][0]).toContain("const answer: number = 42;");
  });

  it("renders readonly task states without unlabeled disabled form controls", () => {
    render(
      <MemoryRouter>
        <MarkdownView body={"- [x] Shipped\n- [ ] Follow up"} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("img", { name: "Completed task" }))
      .toHaveTextContent("☑");
    expect(screen.getByRole("img", { name: "Incomplete task" }))
      .toHaveTextContent("☐");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getByText("Shipped")).toBeInTheDocument();
    expect(screen.getByText("Follow up")).toBeInTheDocument();
  });

  it("keeps highlighted code content inert", () => {
    render(
      <MemoryRouter>
        <MarkdownView body={"```html\n<img src=x onload=alert(1)>\n<script>alert(1)</script>\n```"} />
      </MemoryRouter>,
    );

    const code = document.querySelector("code.hljs");
    expect(code).toBeInTheDocument();
    expect(code).toHaveTextContent("<img src=x onload=alert(1)>");
    expect(code).toHaveTextContent("<script>alert(1)</script>");
    expect(document.querySelector(".markdown-code-block img")).not.toBeInTheDocument();
    expect(document.querySelector(".markdown-code-block script")).not.toBeInTheDocument();
  });

  it("renders Mermaid diagrams through a sanitized lazy SVG", async () => {
    mermaidMock.render.mockResolvedValueOnce({
      svg: [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80">',
        "<script>alert(1)</script>",
        "<style>@import url(https://evil.example/style.css)</style>",
        "<style>body,.dialog-backdrop{display:none!important}</style>",
        '<g onclick="alert(1)" style="fill:red;background:url(javascript:alert(1))">',
        '<a href="javascript:alert(1)"><text>Unsafe label</text></a>',
        '<image href="https://evil.example/pixel.png" onload="alert(1)" />',
        '<use href="https://evil.example/sprite.svg#x" />',
        '<animate attributeName="x" from="0" to="100" />',
        "<text>Graph label</text>",
        "</g>",
        "</svg>",
      ].join(""),
    });

    render(
      <>
        <div className="dialog-backdrop" data-testid="shell-backdrop-sentinel" />
        <MemoryRouter>
          <MarkdownView
            body={"```mermaid\ngraph TD\n  A-->B\n```"}
            sourceLineNumbers
          />
        </MemoryRouter>
      </>,
    );

    expect(await screen.findByText("Graph label")).toBeInTheDocument();
    expect(mermaidMock.initialize).toHaveBeenCalledWith(
      expect.objectContaining({
        flowchart: {
          htmlLabels: false,
          nodeSpacing: 32,
          padding: 10,
          rankSpacing: 36,
          useMaxWidth: false,
        },
        htmlLabels: false,
        securityLevel: "strict",
        startOnLoad: false,
        themeCSS: "",
        themeVariables: {
          fontFamily:
            'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
          fontSize: "13px",
        },
      }),
    );
    expect(mermaidMock.render).toHaveBeenCalledWith(
      expect.stringMatching(/^dtxwiki-mermaid-/),
      expect.stringContaining("graph TD"),
    );
    const diagram = document.querySelector(".mermaid-diagram");
    expect(diagram).toHaveAttribute("data-source-line", "1");
    expect(diagram).toHaveAttribute("data-scroll-sync-anchor", "true");
    expect(diagram).toHaveAttribute("data-source-end-line", "4");
    expect(diagram?.querySelector("script")).not.toBeInTheDocument();
    expect(diagram?.querySelector("style")).not.toBeInTheDocument();
    expect(diagram?.querySelector("image")).not.toBeInTheDocument();
    expect(diagram?.querySelector("use")).not.toBeInTheDocument();
    expect(diagram?.querySelector("animate")).not.toBeInTheDocument();
    expect(diagram?.innerHTML).not.toContain("onclick");
    expect(diagram?.innerHTML).not.toContain("onload");
    expect(diagram?.innerHTML).not.toContain("javascript:");
    expect(diagram?.innerHTML).not.toContain("evil.example");
    expect(getComputedStyle(document.body).display).not.toBe("none");
    expect(getComputedStyle(screen.getByTestId("shell-backdrop-sentinel")).display)
      .not.toBe("none");
  });

  it("normalizes Mermaid root dimensions from a valid viewBox", async () => {
    mermaidMock.render.mockResolvedValueOnce({
      svg: [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-8 -4 640.5 320.25"',
        ' width="100%" height="100%"',
        ' style="width:100%;max-width:640.5px;height:100%;background:#fff">',
        "<text>Intrinsic geometry</text>",
        "</svg>",
      ].join(""),
    });

    render(
      <MemoryRouter>
        <MarkdownView body={"```mermaid\nsequenceDiagram\n  A->>B: Hi\n```"} />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Intrinsic geometry")).toBeInTheDocument();
    const svg = document.querySelector(".mermaid-diagram-svg > svg");
    expect(svg).toHaveAttribute("width", "640.5");
    expect(svg).toHaveAttribute("height", "320.25");
    expect(svg?.getAttribute("style")).not.toMatch(/(?:max-)?width|height/);
    expect(svg?.getAttribute("style")).toContain("background");
  });

  it.each([
    [
      "missing",
      '<svg xmlns="http://www.w3.org/2000/svg" width="100%"><text>Missing geometry</text></svg>',
    ],
    [
      "malformed",
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 NaN 240" width="100%"><text>Malformed geometry</text></svg>',
    ],
    [
      "non-SVG numeric syntax",
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 0x10 240" width="100%"><text>Invalid numeric geometry</text></svg>',
    ],
    [
      "nonpositive",
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 0 240" width="100%"><text>Nonpositive geometry</text></svg>',
    ],
  ])("rejects %s Mermaid intrinsic geometry instead of rendering a collapsed SVG", async (_kind, svg) => {
    mermaidMock.render.mockResolvedValueOnce({
      svg,
    });

    render(
      <MemoryRouter>
        <MarkdownView body={"```mermaid\nstateDiagram-v2\n  A --> B\n```"} />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Mermaid diagram could not be rendered."))
      .toBeInTheDocument();
    expect(document.querySelector(".mermaid-diagram-svg > svg")).not.toBeInTheDocument();
    expect(document.querySelector(".mermaid-diagram-error code"))
      .toHaveTextContent("stateDiagram-v2");
  });

  it("applies visible fallback styles when Mermaid emits class-only flowchart nodes", async () => {
    mermaidMock.render.mockResolvedValueOnce({
      svg: [
        // aria-roledescription is how the sanitizer tells the families apart,
        // and real Mermaid stamps it on every one of them. Centring node
        // labels is correct for flowcharts and wrong for ER/class/state, so a
        // fixture that omits it is not a flowchart as far as the sanitizer is
        // concerned and would not be centred.
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80"' +
          ' aria-roledescription="flowchart-v2">',
        '<path class="flowchart-link" d="M 0 0 L 20 20"></path>',
        '<path class="arrowMarkerPath" d="M 0 0 L 10 5 L 0 10 z"></path>',
        '<g class="node default">',
        '<rect class="background" style="stroke: none"></rect>',
        '<rect class="basic label-container" x="0" y="0" width="100" height="40"></rect>',
        '<text x="50" y="20">Flow label</text>',
        "</g>",
        '<text x="10" y="80">Edge label</text>',
        "</svg>",
      ].join(""),
    });

    render(
      <MemoryRouter>
        <MarkdownView body={"```mermaid\nflowchart TD\n  A[Flow label]\n```"} />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Flow label")).toBeInTheDocument();
    const nodeRect = document.querySelector("rect.label-container");
    expect(nodeRect).toHaveAttribute("fill", "#f8fafc");
    expect(nodeRect).toHaveAttribute("stroke", "#334155");
    expect(document.querySelector("path.flowchart-link"))
      .toHaveAttribute("stroke", "#64748b");
    expect(document.querySelector("rect.background"))
      .toHaveAttribute("fill", "transparent");
    expect(document.querySelector(".node text"))
      .toHaveAttribute("text-anchor", "middle");
    expect(document.querySelector("svg > text"))
      .not.toHaveAttribute("text-anchor");
  });

  it("repaints non-rect Mermaid node shapes when gradient styles are stripped", async () => {
    mermaidMock.render.mockResolvedValueOnce({
      svg: [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80">',
        "<style>.label-container{fill:url(#gradient-1);}</style>",
        '<polygon class="label-container" points="0,20 20,0 40,20 20,40"></polygon>',
        '<circle class="basic label-container" r="20"></circle>',
        '<ellipse class="label-container" rx="30" ry="15"></ellipse>',
        '<path class="basic label-container" d="M 0 0 L 20 0"></path>',
        "<text>Decision label</text>",
        "</svg>",
      ].join(""),
    });

    render(
      <MemoryRouter>
        <MarkdownView body={"```mermaid\ngraph TD\n  B{Check}\n```"} />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Decision label")).toBeInTheDocument();
    const diagram = document.querySelector(".mermaid-diagram");
    expect(diagram?.querySelector("style")).not.toBeInTheDocument();
    for (const selector of [
      "polygon.label-container",
      "circle.label-container",
      "ellipse.label-container",
      "path.label-container",
    ]) {
      const shape = diagram?.querySelector(selector);
      expect(shape).toHaveAttribute("fill", "#f8fafc");
      expect(shape).toHaveAttribute("stroke", "#334155");
    }
  });

  it("strips Mermaid style blocks and supplies safe connector attributes", async () => {
    mermaidMock.render.mockResolvedValueOnce({
      svg: [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80"' +
          ' aria-roledescription="sequence">',
        "<style>.messageLine0{stroke:#334155;fill:none;}</style>",
        // Mermaid 11.16.1 stamps stroke="none" and leaves colour to the
        // stylesheet stripped above; without a fallback only arrowheads show.
        '<line class="messageLine0" x1="0" y1="0" x2="20" y2="20"',
        ' stroke-width="2" stroke="none" style="fill: none;" />',
        '<path class="messageLine1" d="M 0 40 C 20 40 20 60 0 60"',
        ' stroke-width="2" stroke="none" style="stroke-dasharray: 3, 3; fill: none;" />',
        '<line id="authored-message" class="messageLine0" stroke="#ef4444"',
        ' style="stroke: #16a34a;" x1="0" y1="60" x2="20" y2="60" />',
        '<line id="hidden-line" stroke="none" stroke-width="0"',
        ' x1="0" y1="70" x2="20" y2="70" />',
        '<text font-size="16px" font-family="serif"',
        ' style="font:italic 700 16px serif;text-anchor:start">',
        '<tspan font-size="18px" style="font-size:18px">Sequence label</tspan>',
        "</text>",
        "</svg>",
      ].join(""),
    });

    render(
      <MemoryRouter>
        <MarkdownView body={"```mermaid\nsequenceDiagram\n  A->>B: Hi\n```"} />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Sequence label")).toBeInTheDocument();
    const diagram = document.querySelector(".mermaid-diagram");
    expect(diagram?.querySelector("style")).not.toBeInTheDocument();
    for (const selector of ["line.messageLine0", "path.messageLine1"]) {
      const line = diagram?.querySelector(selector);
      expect(line).toHaveAttribute("stroke", "#64748b");
      expect(line).toHaveAttribute("stroke-width", "1.5");
      expect(line).toHaveAttribute("fill", "none");
    }
    expect(
      (diagram?.querySelector("path.messageLine1") as SVGElement).style
        .getPropertyValue("stroke-dasharray"),
    ).toBe("3, 3");
    const authoredMessage = diagram?.querySelector("#authored-message");
    expect(authoredMessage).toHaveAttribute("stroke", "#ef4444");
    expect((authoredMessage as SVGElement).style.getPropertyValue("stroke"))
      .toBe("#16a34a");
    expect(diagram?.querySelector("#hidden-line"))
      .toHaveAttribute("stroke", "none");
    expect(diagram?.querySelector("#hidden-line"))
      .toHaveAttribute("stroke-width", "0");
    const svg = diagram?.querySelector(".mermaid-diagram-svg > svg");
    const text = diagram?.querySelector("text");
    const tspan = diagram?.querySelector("tspan");
    for (const element of [svg, text, tspan]) {
      expect(element).toHaveAttribute("font-size", "13px");
      expect(element).toHaveAttribute(
        "font-family",
        'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      );
      expect((element as SVGElement).style.fontSize).toBe("13px");
      expect((element as SVGElement).style.fontFamily).toContain("Inter");
    }
    expect((text as SVGTextElement).style.fontWeight).toBe("700");
    expect((text as SVGTextElement).style.fontStyle).toBe("italic");
    expect((text as SVGTextElement).style.getPropertyValue("text-anchor"))
      .toBe("start");
  });

  it("opens Mermaid diagrams in a zoom dialog without re-rendering", async () => {
    render(
      <MemoryRouter>
        <MarkdownView body={"```mermaid\ngraph TD\n  A-->B\n```"} />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("button", {
      name: "Expand Mermaid diagram",
    }));

    expect(screen.getByRole("dialog", { name: "Diagram viewer" })).toBeInTheDocument();
    expect(screen.getByRole("toolbar", { name: "Mermaid zoom controls" }))
      .toBeInTheDocument();
    expect(mermaidMock.render).toHaveBeenCalledTimes(1);
  });

  it("invalid syntax shows the fallback without ever calling render", async () => {
    // parse() with suppressErrors returns false on invalid syntax. The gate must
    // bail before render() — render is the only thing that appends a node to
    // <body>, so never calling it means no "Syntax error" orphan can be created.
    mermaidMock.parse.mockResolvedValueOnce(false);

    render(
      <MemoryRouter>
        <MarkdownView body={"```mermaid\nnot a real diagram (((\n```"} />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Mermaid diagram could not be rendered."))
      .toBeInTheDocument();
    expect(document.querySelector(".markdown-code-block"))
      .toHaveTextContent("not a real diagram");
    expect(mermaidMock.render).not.toHaveBeenCalled();
    expect(mermaidMock.parse).toHaveBeenCalledWith(
      expect.stringContaining("not a real diagram"),
      { suppressErrors: true },
    );
  });

  it("cleans up the orphan body node when parse passes but render throws", async () => {
    // Defense-in-depth path: parse OK, render appends its temp <div id="d{id}">
    // to <body> then throws (as the real mermaid leaks on a render-time error).
    // The finally must remove that orphan.
    mermaidMock.render.mockImplementationOnce(async (diagramId: string) => {
      const orphan = document.createElement("div");
      orphan.id = `d${diagramId}`;
      document.body.appendChild(orphan);
      throw new Error("Invalid graph");
    });

    render(
      <MemoryRouter>
        <MarkdownView body={"```mermaid\ngraph TD\n  A-->B\n```"} />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Mermaid diagram could not be rendered."))
      .toBeInTheDocument();
    expect(document.querySelector(".markdown-code-block"))
      .toHaveTextContent("graph TD");
    // The leaked orphan node was removed by the finally cleanup.
    expect(document.querySelector('div[id^="ddtxwiki-mermaid-"]')).toBeNull();
  });

  it("renders LaTeX layout styles and preserves GFM table alignment", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            "Inline math $E=mc^2$.",
            "",
            "$$",
            "x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}",
            "$$",
            "",
            "$$",
            "\\int_{0}^{\\infty} e^{-x^2} dx = \\frac{\\sqrt{\\pi}}{2}",
            "$$",
            "",
            "| Left | Center | Right |",
            "|:--|:--:|--:|",
            "| a | b | c |",
          ].join("\n")}
        />
      </MemoryRouter>,
    );

    expect(document.querySelectorAll(".katex").length).toBeGreaterThanOrEqual(2);
    expect(document.querySelector("math")).not.toBeNull();
    expect(document.querySelector("annotation[encoding='application/x-tex']"))
      .not.toBeNull();
    // KaTeX uses generated inline styles to vertically position fractions,
    // superscripts, and subscripts. Sanitizing after it removes those styles
    // and leaves the equations visibly malformed.
    expect(document.querySelector(".mfrac .frac-line[style]")).not.toBeNull();
    expect(document.querySelector(".msupsub .vlist [style*='top']")).not.toBeNull();

    const headings = Array.from(document.querySelectorAll("th"));
    const tableRegion = screen.getByRole("region", { name: "Table" });
    expect(tableRegion).toHaveClass("markdown-table-scroll");
    expect(tableRegion).toHaveAttribute("tabindex", "0");
    expect(tableRegion.querySelector("table")).not.toBeNull();
    expect(headings[0]).toHaveStyle({ textAlign: "left" });
    expect(headings[1]).toHaveStyle({ textAlign: "center" });
    expect(headings[2]).toHaveStyle({ textAlign: "right" });
  });

  it("keeps allowed MathML while stripping unsafe raw math attributes", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            '<math onclick="alert(1)" style="color: red" display="block">',
            "<semantics>",
            '<mrow><mi>x</mi><mo>=</mo><mn>1</mn></mrow>',
            '<annotation encoding="application/x-tex">x=1</annotation>',
            "<script>alert(1)</script>",
            "</semantics>",
            "</math>",
          ].join("")}
        />
      </MemoryRouter>,
    );

    const math = document.querySelector("math");
    expect(math).not.toBeNull();
    expect(math?.getAttribute("display")).toBe("block");
    expect(math?.hasAttribute("onclick")).toBe(false);
    expect(math?.hasAttribute("style")).toBe(false);
    expect(document.querySelector("script")).not.toBeInTheDocument();
    expect(document.querySelector("annotation[encoding='application/x-tex']"))
      .toHaveTextContent("x=1");
  });

  it("does not recurse indefinitely while transforming deeply nested tokens", () => {
    const body = `${"> ".repeat(160)}[[deep-link|Deep Link]]`;

    render(
      <MemoryRouter>
        <MarkdownView body={body} />
      </MemoryRouter>,
    );

    expect(screen.getByText(/\[\[deep-link\|Deep Link\]\]/)).toBeInTheDocument();
  });

  it("renders decision and action comment fences as read-only blocks", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            "<!-- decision id=dec-001 status=approved owner=@alice -->",
            "Use SQLite for local development.",
            "<!-- /decision -->",
            "",
            "<!-- action id=act-001 owner=@bob due=2026-06-01 -->",
            "- Prepare migration checklist",
            "<!-- /action -->",
          ].join("\n")}
          sourceLineNumbers
        />
      </MemoryRouter>,
    );

    const decision = document.querySelector("decision-block");
    expect(decision).not.toBeNull();
    expect(decision).toHaveAttribute("data-decision-id", "dec-001");
    expect(decision).toHaveAttribute("data-status", "approved");
    expect(decision).toHaveAttribute("data-owner", "@alice");
    expect(decision).toHaveAttribute("data-source-line", "1");
    expect(decision).toHaveAttribute("data-source-end-line", "3");
    expect(decision).toHaveAttribute("data-scroll-sync-anchor", "true");
    expect(decision).toHaveClass("decision-block");
    expect(decision).toHaveTextContent("Use SQLite for local development.");

    const action = document.querySelector("action-block");
    expect(action).not.toBeNull();
    expect(action).toHaveAttribute("data-action-id", "act-001");
    expect(action).toHaveAttribute("data-owner", "@bob");
    expect(action).toHaveAttribute("data-due", "2026-06-01");
    expect(action).toHaveAttribute("data-source-line", "5");
    expect(action).toHaveAttribute("data-source-end-line", "7");
    expect(action).toHaveAttribute("data-scroll-sync-anchor", "true");
    expect(action).toHaveClass("action-block");
    expect(action).toHaveTextContent("Prepare migration checklist");
    expect(document.querySelector('[data-scroll-anchor="top"]')).toHaveAttribute(
      "data-source-line",
      "1",
    );
    expect(document.querySelector('[data-scroll-anchor="bottom"]')).toHaveAttribute(
      "data-source-line",
      "8",
    );
  });

  it("replaces authored scroll metadata with trusted top-level source ranges", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={[
            '<div data-source-line="999" data-source-end-line="999">',
            '<span data-scroll-sync-anchor="true" data-source-line="999">Raw</span>',
            "</div>",
            "",
            "Actual paragraph",
          ].join("\n")}
          sourceLineNumbers
        />
      </MemoryRouter>,
    );

    const rawBlock = screen.getByText("Raw").closest("div");
    expect(rawBlock).toHaveAttribute("data-source-line", "1");
    expect(rawBlock).toHaveAttribute("data-source-end-line", "3");
    expect(rawBlock).toHaveAttribute("data-scroll-sync-anchor", "true");
    expect(screen.getByText("Raw")).not.toHaveAttribute(
      "data-scroll-sync-anchor",
    );
    expect(screen.getByText("Actual paragraph")).toHaveAttribute(
      "data-source-line",
      "5",
    );
  });

  it("keeps raw token alias text safe", () => {
    render(
      <MemoryRouter>
        <MarkdownView body={"[[safe-page|Unsafe script>alert(1) text]]"} />
      </MemoryRouter>,
    );

    // P10.1: unresolved (no wikilinkTargets/wikilinkPages supplied) renders
    // broken now that /pages/{slug} is gone — the alias-text sanitization is
    // this test's actual subject, independent of the href value.
    expect(screen.getByRole("button", { name: "Unsafe script>alert(1) text" }))
      .toHaveClass("unavailable-reference__trigger");
    expect(document.querySelector("script")).not.toBeInTheDocument();
  });

  it("highlights exact and fuzzy comment anchors without marking distant or orphaned anchors", () => {
    render(
      <MemoryRouter>
        <MarkdownView
          body={"Exact anchor and fuzzy anchor stay visible. Distant anchor is panel-only."}
          comments={[
            inlineComment("c-exact", "exact", "Exact anchor"),
            inlineComment("c-fuzzy", "fuzzy", "fuzzy anchor"),
            inlineComment("c-distant", "fuzzy_distant", "Distant anchor"),
            inlineComment("c-orphan", "orphan", null),
          ]}
        />
      </MemoryRouter>,
    );

    expect(document.querySelector('mark.anchor--exact[data-comment-id="c-exact"]'))
      .toHaveTextContent("Exact anchor");
    expect(document.querySelector('mark.anchor--fuzzy[data-comment-id="c-fuzzy"]'))
      .toHaveTextContent("fuzzy anchor");
    expect(document.querySelector('[data-comment-id="c-distant"]')).not.toBeInTheDocument();
    expect(document.querySelector('[data-comment-id="c-orphan"]')).not.toBeInTheDocument();
  });

  it("uses resolved offsets to disambiguate repeated comment anchor text", () => {
    const body = "Repeated anchor appears first. Repeated anchor appears second.";
    const secondOffset = body.indexOf("Repeated anchor", "Repeated anchor".length);

    render(
      <MemoryRouter>
        <MarkdownView
          body={body}
          comments={[
            inlineComment("c-second", "exact", "Repeated anchor", secondOffset),
          ]}
        />
      </MemoryRouter>,
    );

    const marks = document.querySelectorAll(
      'mark.anchor--exact[data-comment-id="c-second"]',
    );
    expect(marks).toHaveLength(1);
    expect(marks[0]).toHaveTextContent("Repeated anchor");
    expect(marks[0].previousSibling?.textContent).toBe(
      "Repeated anchor appears first. ",
    );
  });

  it("caps rendered inline comment highlights for large comment sets", () => {
    const anchors = Array.from(
      { length: 55 },
      (_, index) => `anchor-${String(index).padStart(2, "0")}-text`,
    );
    render(
      <MemoryRouter>
        <MarkdownView
          body={anchors.join(" ")}
          comments={anchors.map((anchor, index) =>
            inlineComment(`c-${index}`, "exact", anchor),
          )}
        />
      </MemoryRouter>,
    );

    expect(document.querySelectorAll("mark.anchor")).toHaveLength(50);
    expect(document.querySelector('[data-comment-id="c-54"]')).not.toBeInTheDocument();
  });
});

type InlineCommentStatus = "exact" | "fuzzy" | "fuzzy_distant" | "orphan";

function LocationStateProbe() {
  const location = useLocation();
  return <output data-testid="markdown-location-state">{JSON.stringify(location.state)}</output>;
}

function inlineComment(
  id: string,
  status: InlineCommentStatus,
  resolvedText: string | null,
  charOffset = 0,
) {
  return {
    id,
    page_id: "p1",
    review_item_id: `review-${id}`,
    change_set_id: `change-set-${id}`,
    change_operation_id: `operation-${id}`,
    status: "open",
    risk_level: "medium",
    note: "Review note",
    anchor: {
      anchor_text: resolvedText ?? "Original orphan anchor",
      context_before: "",
      context_after: "",
      version_id: "v1",
      char_offset_hint: 0,
    },
    resolved: {
      char_offset: status === "orphan" ? null : charOffset,
      end_offset:
        status === "orphan" || resolvedText === null
          ? null
          : charOffset + resolvedText.length,
      status,
      resolved_text: resolvedText,
    },
    created_at: "2026-05-08T00:00:00Z",
    updated_at: "2026-05-08T00:00:00Z",
  };
}
