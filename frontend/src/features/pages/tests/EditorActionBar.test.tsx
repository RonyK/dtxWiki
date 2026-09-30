import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EditorActionBar } from "../EditorActionBar";

describe("EditorActionBar", () => {
  it("renders a persistent desktop toolbar with a top-level Attach action", async () => {
    const onCommand = vi.fn();
    const user = userEvent.setup();
    render(<EditorActionBar mode="source" onCommand={onCommand} />);

    const toolbar = screen.getByRole("toolbar", { name: "Editor actions" });
    expect(within(toolbar).getByRole("button", { name: "Bold" })).toBeVisible();
    expect(within(toolbar).getByRole("button", { name: "Image" })).toBeVisible();
    const attach = within(toolbar).getByRole("button", { name: "Attach files" });
    expect(attach).toBeVisible();
    expect(attach).toHaveTextContent("Attach files");
    expect(within(toolbar).getByRole("button", { name: "Heading" })
      .querySelector(".editor-command-glyph.is-heading")).toHaveTextContent("H");
    expect(within(toolbar).getByRole("button", { name: "Bold" })
      .querySelector(".editor-command-glyph.is-bold")).toHaveTextContent("B");
    expect(within(toolbar).getByRole("button", { name: "Italic" })
      .querySelector(".editor-command-glyph.is-italic")).toHaveTextContent("I");
    expect(within(toolbar).getByRole("button", { name: "Strikethrough" })
      .querySelector(".editor-command-glyph.is-strikethrough")).toHaveTextContent("S");
    expect(within(toolbar).getByRole("button", {
      name: "More editor actions",
    })).toBeVisible();

    await user.click(attach);
    expect(onCommand).toHaveBeenCalledWith(
      "attach",
      expect.objectContaining({
        attachment: "files",
        kind: "async-attachment",
      }),
    );
  });

  it("keeps only frequent actions visible on mobile and moves the rest to More", async () => {
    const user = userEvent.setup();
    render(
      <EditorActionBar isMobile mode="source" onCommand={vi.fn()} />,
    );

    const toolbar = screen.getByRole("toolbar", { name: "Editor actions" });
    expect(within(toolbar).getByRole("button", { name: "Heading" })).toBeVisible();
    expect(within(toolbar).getByRole("button", { name: "Bold" })).toBeVisible();
    expect(within(toolbar).getByRole("button", { name: "Link" })).toBeVisible();
    expect(within(toolbar).getByRole("button", { name: "Attach files" })).toBeVisible();
    expect(within(toolbar).queryByRole("button", { name: "Italic" })).toBeNull();
    expect(within(toolbar).queryByRole("button", { name: "Image" })).toBeNull();

    await user.click(
      within(toolbar).getByRole("button", { name: "More editor actions" }),
    );
    const menu = screen.getByRole("menu", { name: "More editor actions" });
    const italic = within(menu).getByRole("menuitem", { name: "Italic" });
    const image = within(menu).getByRole("menuitem", { name: "Image" });
    expect(italic).toBeVisible();
    expect(italic).toHaveTextContent("Italic");
    expect(italic.querySelector(".editor-command-glyph.is-italic")).toBeInTheDocument();
    expect(image).toBeVisible();
    expect(image).toHaveTextContent("Image");
    expect(image.querySelector("svg")).toBeInTheDocument();
  });

  it("uses resolved labels for visible action and menu copy", async () => {
    const user = userEvent.setup();
    const translated = new Map([
      ["editor.commands.attachFiles", "파일 첨부"],
      ["editor.commands.italic", "기울임꼴"],
    ]);
    render(
      <EditorActionBar
        isMobile
        labels={{
          more: "편집기 작업 더 보기",
          moreMenu: "편집기 작업 더 보기",
          moreShort: "더 보기",
          toolbar: "편집기 작업",
        }}
        mode="source"
        onCommand={vi.fn()}
        resolveLabel={(key, fallback) => translated.get(key) ?? fallback}
      />,
    );

    const toolbar = screen.getByRole("toolbar", { name: "편집기 작업" });
    expect(within(toolbar).getByRole("button", { name: "파일 첨부" }))
      .toHaveTextContent("파일 첨부");
    await user.click(within(toolbar).getByRole("button", { name: "편집기 작업 더 보기" }));
    expect(within(screen.getByRole("menu", { name: "편집기 작업 더 보기" }))
      .getByRole("menuitem", { name: "기울임꼴" })).toHaveTextContent("기울임꼴");
  });

  it("uses one toolbar tab stop and supports Arrow, Home, and End roving focus", async () => {
    const user = userEvent.setup();
    render(<EditorActionBar mode="source" onCommand={vi.fn()} />);
    const toolbar = screen.getByRole("toolbar", { name: "Editor actions" });
    const buttons = within(toolbar).getAllByRole("button");

    expect(buttons.filter((button) => button.tabIndex === 0)).toHaveLength(1);
    buttons[0].focus();
    expect(buttons[0]).toHaveFocus();

    await user.keyboard("{ArrowRight}");
    expect(buttons[1]).toHaveFocus();
    expect(buttons.filter((button) => button.tabIndex === 0)).toHaveLength(1);

    await user.keyboard("{End}");
    expect(buttons.at(-1)).toHaveFocus();
    await user.keyboard("{Home}");
    expect(buttons[0]).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(buttons.at(-1)).toHaveFocus();
  });

  it("exposes mixed pressed state and a localized Reading-mode disabled reason", async () => {
    const onCommand = vi.fn();
    const user = userEvent.setup();
    render(
      <EditorActionBar
        commandStates={{ bold: "mixed" }}
        labels={{
          readingDisabledReason: "Reading is view-only.",
          toolbar: "Formatting actions",
        }}
        mode="reading"
        onCommand={onCommand}
        resolveLabel={(key, fallback) =>
          key === "editor.commands.bold" ? "Strong" : fallback
        }
      />,
    );

    const toolbar = screen.getByRole("toolbar", { name: "Formatting actions" });
    const bold = within(toolbar).getByRole("button", { name: "Strong" });
    expect(bold).toHaveAttribute("aria-pressed", "mixed");
    expect(bold).toHaveAttribute("aria-disabled", "true");
    expect(bold).toHaveAccessibleDescription("Reading is view-only.");
    expect(bold).toHaveAttribute("title", expect.stringContaining("Reading is view-only."));

    await user.click(bold);
    expect(onCommand).not.toHaveBeenCalled();
  });

  it("prevents pointer focus theft so the editor selection survives command activation", () => {
    const onCommand = vi.fn();
    const onPreserveSelection = vi.fn();
    render(
      <>
        <textarea aria-label="Markdown source" defaultValue="alpha beta" />
        <EditorActionBar
          mode="source"
          onCommand={onCommand}
          onPreserveSelection={onPreserveSelection}
        />
      </>,
    );
    const editor = screen.getByRole("textbox", {
      name: "Markdown source",
    }) as HTMLTextAreaElement;
    const bold = screen.getByRole("button", { name: "Bold" });
    editor.focus();
    editor.setSelectionRange(0, 5);

    const mouseDown = new MouseEvent("mousedown", {
      bubbles: true,
      button: 0,
      cancelable: true,
    });
    fireEvent(bold, mouseDown);
    expect(mouseDown.defaultPrevented).toBe(true);
    expect(editor).toHaveFocus();
    expect(editor.selectionStart).toBe(0);
    expect(editor.selectionEnd).toBe(5);
    expect(onPreserveSelection).toHaveBeenCalledTimes(1);

    fireEvent.click(bold);
    expect(onCommand).toHaveBeenCalledWith(
      "bold",
      expect.objectContaining({ kind: "text-mutation" }),
    );
  });

  it("restores More focus after Escape, click-away, and resize", async () => {
    const user = userEvent.setup();
    render(<EditorActionBar isMobile mode="source" onCommand={vi.fn()} />);
    const more = screen.getByRole("button", { name: "More editor actions" });

    await user.click(more);
    let menu = screen.getByRole("menu", { name: "More editor actions" });
    await waitFor(() => expect(within(menu).getAllByRole("menuitem")[0]).toHaveFocus());
    fireEvent.keyDown(within(menu).getAllByRole("menuitem")[0], { key: "Escape" });
    await waitFor(() => expect(more).toHaveFocus());
    expect(screen.queryByRole("menu")).toBeNull();

    await user.click(more);
    menu = screen.getByRole("menu", { name: "More editor actions" });
    await waitFor(() => expect(within(menu).getAllByRole("menuitem")[0]).toHaveFocus());
    fireEvent.mouseDown(document.body);
    await waitFor(() => expect(more).toHaveFocus());
    expect(screen.queryByRole("menu")).toBeNull();

    await user.click(more);
    menu = screen.getByRole("menu", { name: "More editor actions" });
    await waitFor(() => expect(within(menu).getAllByRole("menuitem")[0]).toHaveFocus());
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(more).toHaveFocus());
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
