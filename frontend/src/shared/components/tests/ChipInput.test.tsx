import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChipInput } from "../ChipInput";

describe("ChipInput", () => {
  it("adds comma and enter separated chips and suppresses duplicates", () => {
    const handleChange = vi.fn();

    render(
      <ChipInput
        ariaLabel="Tags"
        onChange={handleChange}
        placeholder="research"
        value={["Existing"]}
      />,
    );

    const input = screen.getByLabelText("Tags");
    fireEvent.change(input, { target: { value: "New tag" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.change(input, { target: { value: "existing, Second" } });
    fireEvent.keyDown(input, { key: "," });

    expect(handleChange).toHaveBeenNthCalledWith(1, ["Existing", "New tag"]);
    expect(handleChange).toHaveBeenNthCalledWith(2, ["Existing", "Second"]);
  });

  it("removes chips and inserts suggestions", () => {
    const handleChange = vi.fn();

    render(
      <ChipInput
        ariaLabel="Tags"
        onChange={handleChange}
        onQueryChange={() => undefined}
        suggestions={["Architecture"]}
        value={["Existing"]}
      />,
    );

    fireEvent.click(screen.getByLabelText("Remove Existing"));
    expect(handleChange).toHaveBeenCalledWith([]);

    fireEvent.change(screen.getByLabelText("Tags"), { target: { value: "arch" } });
    fireEvent.click(screen.getByRole("option", { name: "Architecture" }));
    expect(handleChange).toHaveBeenLastCalledWith(["Existing", "Architecture"]);
  });

  it("can preserve leading hash characters and use a custom item length", () => {
    const handleChange = vi.fn();

    render(
      <ChipInput
        ariaLabel="Aliases"
        maxItemLength={12}
        onChange={handleChange}
        stripLeadingHash={false}
        value={[]}
      />,
    );

    const input = screen.getByLabelText("Aliases");
    fireEvent.change(input, { target: { value: "#Hash Alias That Is Long" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(handleChange).toHaveBeenCalledWith(["#Hash Alias"]);
  });

  it("reports an overlong item without silently truncating when truncation is disabled", () => {
    const handleChange = vi.fn();
    const handleTooLong = vi.fn();

    render(
      <ChipInput
        ariaLabel="Extensions"
        maxItemLength={5}
        onChange={handleChange}
        onItemTooLong={handleTooLong}
        stripLeadingHash={false}
        truncateItems={false}
        value={[]}
      />,
    );

    const input = screen.getByLabelText("Extensions");
    fireEvent.change(input, { target: { value: ".longer" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(handleTooLong).toHaveBeenCalledWith(".longer");
    expect(handleChange).not.toHaveBeenCalled();
    expect(input).toHaveValue(".longer");
  });

  it("exposes validation semantics for a rejected overlong item", () => {
    const handleChange = vi.fn();
    const handleTooLong = vi.fn();

    render(
      <>
        <p id="aliases-hint">Alias requirements</p>
        <p id="aliases-error">Alias is too long</p>
        <ChipInput
          ariaDescribedBy="aliases-hint aliases-error"
          ariaInvalid
          ariaLabel="Aliases"
          maxItemLength={12}
          onChange={handleChange}
          onItemTooLong={handleTooLong}
          stripLeadingHash={false}
          truncateItems={false}
          value={[]}
        />
      </>,
    );

    const input = screen.getByLabelText("Aliases");
    fireEvent.change(input, { target: { value: "#Hash Alias That Is Long" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("Alias requirements Alias is too long");
    expect(handleTooLong).toHaveBeenCalledWith("#Hash Alias That Is Long");
    expect(handleChange).not.toHaveBeenCalled();
  });

  it("does not commit the query while an IME composition is active", () => {
    const handleChange = vi.fn();

    render(
      <ChipInput
        ariaLabel="Aliases"
        onChange={handleChange}
        stripLeadingHash={false}
        value={[]}
      />,
    );

    const input = screen.getByLabelText("Aliases");
    fireEvent.change(input, { target: { value: "한글" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });

    expect(handleChange).not.toHaveBeenCalled();
    expect(input).toHaveValue("한글");
  });

  it("supports accessible inline editing and reports editing state", () => {
    const handleChange = vi.fn();
    const handleEditingChange = vi.fn();

    render(
      <ChipInput
        ariaDescribedBy="extension-help"
        ariaLabel="Extensions"
        editableItems
        getEditAriaLabel={(item) => `Edit extension ${item}`}
        getRemoveAriaLabel={(item) => `Remove extension ${item}`}
        onChange={handleChange}
        onEditingChange={handleEditingChange}
        stripLeadingHash={false}
        value={[".md", ".markdown"]}
      />,
    );

    expect(screen.getByLabelText("Extensions")).toHaveAttribute(
      "aria-describedby",
      "extension-help",
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit extension .md" }));
    const editor = screen.getByRole("textbox", { name: "Edit extension .md" });
    fireEvent.change(editor, { target: { value: ".mdx" } });
    fireEvent.keyDown(editor, { key: "Enter" });

    expect(handleChange).toHaveBeenCalledWith([".mdx", ".markdown"]);
    expect(handleEditingChange).toHaveBeenNthCalledWith(1, true);
    expect(handleEditingChange).toHaveBeenNthCalledWith(2, false);
    expect(screen.getByRole("button", { name: "Remove extension .markdown" }))
      .toBeInTheDocument();
  });

  it("clears editing state when the active chip is removed", () => {
    const handleChange = vi.fn();
    const handleEditingChange = vi.fn();

    render(
      <ChipInput
        ariaLabel="Extensions"
        editableItems
        getEditAriaLabel={(item) => `Edit extension ${item}`}
        getRemoveAriaLabel={(item) => `Remove extension ${item}`}
        onChange={handleChange}
        onEditingChange={handleEditingChange}
        value={[".md", ".markdown"]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Edit extension .md" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove extension .md" }));

    expect(handleChange).toHaveBeenLastCalledWith([".markdown"]);
    expect(handleEditingChange).toHaveBeenNthCalledWith(1, true);
    expect(handleEditingChange).toHaveBeenNthCalledWith(2, false);
  });

  it("keeps a persistent placeholder after chips are added", () => {
    const handleChange = vi.fn();

    render(
      <ChipInput
        ariaLabel="Extensions"
        onChange={handleChange}
        persistentPlaceholder
        placeholder="+ Add extension"
        value={[".md"]}
      />,
    );

    expect(screen.getByLabelText("Extensions")).toHaveAttribute(
      "placeholder",
      "+ Add extension",
    );
  });

  it("reports duplicates and preserves the duplicate query", () => {
    const handleChange = vi.fn();
    const handleDuplicate = vi.fn();

    render(
      <ChipInput
        ariaLabel="Extensions"
        onChange={handleChange}
        onDuplicate={handleDuplicate}
        value={[".md"]}
      />,
    );

    const input = screen.getByLabelText("Extensions");
    fireEvent.change(input, { target: { value: ".MD" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(handleDuplicate).toHaveBeenCalledWith(".MD");
    expect(handleChange).not.toHaveBeenCalled();
    expect(input).toHaveValue(".MD");
  });

  it("preserves and selects a rejected query", () => {
    const handleChange = vi.fn(() => false);

    render(<ChipInput ariaLabel="Extensions" onChange={handleChange} value={[]} />);

    const input = screen.getByLabelText("Extensions");
    fireEvent.change(input, { target: { value: ".invalid" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(input).toHaveValue(".invalid");
    expect(input).toHaveProperty("selectionStart", 0);
    expect(input).toHaveProperty("selectionEnd", ".invalid".length);
  });

  it("commits a pending query when the input blurs", () => {
    const handleChange = vi.fn();

    render(<ChipInput ariaLabel="Extensions" onChange={handleChange} value={[]} />);

    const input = screen.getByLabelText("Extensions");
    fireEvent.change(input, { target: { value: ".md" } });
    fireEvent.blur(input);

    expect(handleChange).toHaveBeenCalledWith([".md"]);
    expect(input).toHaveValue("");
  });
});
