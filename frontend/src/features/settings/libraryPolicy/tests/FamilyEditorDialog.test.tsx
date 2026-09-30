import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  LibraryFormatCapability,
  LibraryFormatFamilyPreset,
} from "../../../../shared/media/libraryFormatCapabilities";
import { FamilyEditorDialog } from "../FamilyEditorDialog";
import type {
  LibraryPolicyBuiltinFormatOverrides,
  LibraryPolicyCustomFormat,
  LibraryPolicyFormatFamily,
} from "../model";

describe("Format Family alias-group editor", () => {
  it("marks persisted preset families as in use without disabling reuse", async () => {
    const user = userEvent.setup();
    const modifiedPresetFamily: LibraryPolicyFormatFamily = {
      family_id: "family-documents",
      name: "Office documents",
      origin: "preset",
      preset_id: "document",
      preset_revision: 1,
      preset_baseline: {
        preset_id: "document",
        preset_revision: 1,
        format_ids: ["docx"],
        format_extensions: { docx: [".docx"] },
      },
      format_ids: ["txt"],
      max_upload_bytes: null,
    };
    renderFamilyEditor({
      families: [modifiedPresetFamily],
      formats: [
        format("docx", "Word DOCX", [".docx"]),
        format("txt", "Plain text", [".txt"]),
        format("png", "PNG", [".png"]),
      ],
      presets: [
        preset("document", "Documents", ["docx"], { docx: [".docx"] }),
        preset("image", "Images", ["png"], { png: [".png"] }),
      ],
    });

    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(dialog).getByRole("radio", { name: "Preset" }));

    const usedOption = within(dialog).getByRole("option", {
      name: "Documents — In use",
    });
    expect(usedOption).not.toBeDisabled();
    expect(within(dialog).getByRole("option", { name: "Images" })).not.toBeDisabled();
  });

  it("keeps the current modified preset marked as in use while editing", () => {
    const modifiedPresetFamily: LibraryPolicyFormatFamily = {
      family_id: "family-documents",
      name: "Office documents",
      origin: "preset",
      preset_id: "document",
      preset_revision: 1,
      preset_baseline: {
        preset_id: "document",
        preset_revision: 1,
        format_ids: ["docx"],
        format_extensions: { docx: [".docx"] },
      },
      format_ids: ["txt"],
      max_upload_bytes: 25_000_000,
    };
    renderFamilyEditor({
      formats: [
        format("docx", "Word DOCX", [".docx"]),
        format("txt", "Plain text", [".txt"]),
      ],
      initialFamily: modifiedPresetFamily,
      presets: [preset("document", "Documents", ["docx"], { docx: [".docx"] })],
    });

    const dialog = screen.getByRole("dialog", { name: "Edit format family" });
    const presetSelect = within(dialog).getByLabelText("Family preset");
    expect(presetSelect).toHaveValue("document");
    expect(within(dialog).getByRole("option", {
      name: "Documents — In use",
    })).not.toBeDisabled();
  });

  it("adds a registered built-in through the combobox keyboard model", async () => {
    const user = userEvent.setup();
    renderFamilyEditor({
      formats: [
        format("docx", "Word DOCX", [".docx"]),
        format("xlsx", "Excel XLSX", [".xlsx"]),
      ],
    });
    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    expect(dialog).toHaveTextContent(
      "Registered extensions add existing formats; unknown extensions continue to format registration.",
    );
    const finder = within(dialog).getByRole("combobox", {
      name: "Add formats",
    });

    await user.type(finder, "docx");
    const option = within(dialog).getByRole("option", { name: /Word DOCX/ });
    expect(finder).toHaveAttribute("aria-expanded", "true");
    expect(finder).toHaveAttribute("aria-controls", option.parentElement?.id);

    await user.keyboard("{ArrowDown}");
    expect(finder).toHaveAttribute("aria-activedescendant", option.id);
    expect(option).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");

    expect(within(aliasGroupsList(dialog)).getByText("Word DOCX", { exact: true }))
      .toBeInTheDocument();
    expect(finder).toHaveValue("");
    expect(finder).toHaveAttribute("aria-expanded", "true");

    await user.type(finder, "xlsx");
    await user.keyboard("{Enter}");
    expect(within(aliasGroupsList(dialog)).getByText("Excel XLSX", { exact: true }))
      .toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Add format family" })).toBeInTheDocument();
  });

  it("renders the simplified sections in order without a standalone custom-format section", () => {
    renderFamilyEditor();
    const dialog = screen.getByRole("dialog", { name: "Add format family" });

    expect(Array.from(dialog.querySelectorAll<HTMLElement>(
      "[data-family-editor-section]",
    )).map((section) => section.dataset.familyEditorSection)).toEqual([
      "start-from",
      "family-name",
      "upload-limit",
      "add-formats",
      "format-alias-groups",
    ]);
    expect(within(dialog).queryByRole("heading", {
      name: "Workspace custom formats",
    })).not.toBeInTheDocument();
    expect(within(dialog).getByText("Add formats", { selector: "strong" }))
      .toBeInTheDocument();
    expect(within(dialog).getByText("Format alias groups", { selector: "strong" }))
      .toBeInTheDocument();
  });

  it("reuses an exact registered custom extension without opening or duplicating it", async () => {
    const user = userEvent.setup();
    const notes = customFormat("custom-notes", "Notes", [".note"]);
    renderFamilyEditor({ customFormats: [notes] });
    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    const addFormats = within(dialog).getByRole("combobox", { name: "Add formats" });

    await user.type(addFormats, ".note{Enter}");

    expect(within(aliasGroupsList(dialog)).getByText("Notes", { exact: true }))
      .toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Format name")).not.toBeInTheDocument();
    const manageFormats = within(dialog).getByRole("button", {
      name: "Manage custom formats (1)",
    });
    expect(manageFormats).toHaveAttribute("aria-expanded", "false");

    await user.click(manageFormats);
    const customCatalog = within(dialog).getByRole("list", {
      name: "Registered custom formats",
    });
    expect(within(customCatalog).getAllByRole("listitem")).toHaveLength(1);
    expect(within(customCatalog).getByText("In this family")).toBeInTheDocument();
  });

  it("does not accept a fuzzy suggestion on Enter until it is explicitly selected", async () => {
    const user = userEvent.setup();
    renderFamilyEditor({
      formats: [format("docx", "Word DOCX", [".docx"])],
    });
    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    const addFormats = within(dialog).getByRole("combobox", { name: "Add formats" });

    await user.type(addFormats, "Word D");
    expect(within(dialog).getByRole("option", { name: /Word DOCX/ }))
      .toHaveAttribute("aria-selected", "false");
    await user.keyboard("{Enter}");

    expect(addFormats).toHaveValue("Word D");
    expect(within(aliasGroupsList(dialog)).queryByText("Word DOCX", { exact: true }))
      .not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Format name")).not.toBeInTheDocument();
  });

  it("opens inline registration with an unknown extension prefilled", async () => {
    const user = userEvent.setup();
    renderFamilyEditor();
    const dialog = screen.getByRole("dialog", { name: "Add format family" });

    await user.type(
      within(dialog).getByRole("combobox", { name: "Add formats" }),
      "newext{Enter}",
    );

    expect(within(dialog).getByRole("group", { name: "Register custom format" }))
      .toBeInTheDocument();
    expect(within(dialog).getByText(".newext", { exact: true })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Format name")).toHaveValue("");
  });

  it("clears hidden raw source and restores single-format registration mode", async () => {
    const user = userEvent.setup();
    renderFamilyEditor();
    const dialog = screen.getByRole("dialog", { name: "Add format family" });

    await user.click(within(dialog).getByRole("button", {
      name: "Register custom format",
    }));
    const rawDisclosure = within(dialog).getByRole("button", {
      name: "Show raw values",
    });
    const formatName = within(dialog).getByLabelText("Format name");
    const extensionAliases = within(dialog).getByLabelText("Extension aliases");

    expect(rawDisclosure).toHaveAttribute("aria-expanded", "false");
    expect(within(dialog).queryByLabelText("Raw source (add / register)"))
      .not.toBeInTheDocument();
    expect(formatName).toBeEnabled();
    expect(extensionAliases).toBeEnabled();

    await user.click(rawDisclosure);
    const rawSource = within(dialog).getByLabelText("Raw source (add / register)");
    await user.type(rawSource, ".temporary");
    expect(formatName).toBeDisabled();
    expect(extensionAliases).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Add or register groups" }))
      .toBeEnabled();

    await user.click(rawDisclosure);
    expect(rawDisclosure).toHaveAttribute("aria-expanded", "false");
    expect(within(dialog).queryByLabelText("Raw source (add / register)"))
      .not.toBeInTheDocument();
    expect(formatName).toBeEnabled();
    expect(extensionAliases).toBeEnabled();
    expect(within(dialog).queryByRole("button", { name: "Add or register groups" }))
      .not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Register format" }))
      .toBeDisabled();

    await user.click(rawDisclosure);
    expect(within(dialog).getByLabelText("Raw source (add / register)"))
      .toHaveValue("");
  });

  it("registers bounded raw source, then edits extensions through the shared chip input", async () => {
    const user = userEvent.setup();
    renderFamilyEditor();
    const dialog = screen.getByRole("dialog", { name: "Add format family" });

    await user.click(within(dialog).getByRole("button", {
      name: "Register custom format",
    }));
    const rawDisclosure = within(dialog).getByRole("button", {
      name: "Show raw values",
    });
    expect(rawDisclosure).toHaveAttribute("aria-expanded", "false");
    expect(within(dialog).queryByLabelText("Raw source (add / register)"))
      .not.toBeInTheDocument();

    await user.click(rawDisclosure);
    expect(rawDisclosure).toHaveAttribute("aria-expanded", "true");
    const rawSource = within(dialog).getByLabelText("Raw source (add / register)");
    expect(rawSource).toHaveAttribute("maxLength", "8192");
    await user.type(rawSource, ".do");
    await user.click(within(dialog).getByRole("button", { name: "Add or register groups" }));

    expect(within(dialog).getByRole("button", { name: "Edit alias group do" }))
      .toBeInTheDocument();
    const aliases = within(dialog).getByRole("textbox", { name: "Extensions for do" });
    await user.type(aliases, "exe{Enter}");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Blocked extension: .exe");
    expect(within(dialog).queryByText(".exe", { exact: true })).not.toBeInTheDocument();
    expect(aliases).toHaveValue("exe");
    await user.clear(aliases);
    await user.type(aliases, "foo{Enter}");
    expect(within(dialog).getByText(".foo", { exact: true })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", {
      name: "Remove extension .foo",
    }));
    expect(within(dialog).queryByText(".foo", { exact: true })).not.toBeInTheDocument();
  });

  it("rejects mixed and ambiguous registered aliases atomically in raw source", async () => {
    const user = userEvent.setup();
    renderFamilyEditor({
      formats: [
        format("word-doc", "Word DOC", [".doc"]),
        format("word-docx", "Word DOCX", [".docx"]),
        format("hdf4", "HDF4", [".hdf"]),
        format("hdf5", "HDF5", [".hdf"]),
      ],
    });
    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(dialog).getByRole("button", {
      name: "Register custom format",
    }));
    await user.click(within(dialog).getByRole("button", {
      name: "Show raw values",
    }));
    const rawSource = within(dialog).getByLabelText("Raw source (add / register)");

    await user.type(rawSource, ".doc,.docx");
    await user.click(within(dialog).getByRole("button", { name: "Add or register groups" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "All extensions in one alias group must belong to the same format",
    );
    expect(within(aliasGroupsList(dialog)).queryByText("Word DOC", { exact: true }))
      .not.toBeInTheDocument();

    await user.clear(rawSource);
    await user.type(rawSource, ".hdf");
    await user.click(within(dialog).getByRole("button", { name: "Add or register groups" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      ".hdf matches more than one registered format (HDF4, HDF5)",
    );

    await user.clear(rawSource);
    await user.type(rawSource, ".html");
    await user.click(within(dialog).getByRole("button", { name: "Add or register groups" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      ".html cannot be registered",
    );
  });

  it("rejects an unsafe alias in single custom-format registration", async () => {
    const user = userEvent.setup();
    renderFamilyEditor();
    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(dialog).getByRole("button", { name: "Register custom format" }));
    await user.type(within(dialog).getByLabelText("Format name"), "Active content");
    await user.type(within(dialog).getByLabelText("Extension aliases"), ".svg{Enter}");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Blocked extension: .svg");
    expect(within(dialog).getByRole("button", { name: "Register format" })).toBeDisabled();
  });

  it("blocks inline edits that transfer a stable extension owner", async () => {
    const user = userEvent.setup();
    const baselineOwner = customFormat("custom-archive", "Archive", [".foo"]);
    const currentOwner = customFormat("custom-archive", "Archive", [".archive"]);
    const other = customFormat("custom-other", "Other", [".bar"]);
    const family = customFamily("family-other", "Other files", ["custom-other"]);
    renderFamilyEditor({
      baselineCustomFormats: [baselineOwner, other],
      customFormats: [currentOwner, other],
      initialFamily: family,
    });

    const dialog = screen.getByRole("dialog", { name: "Edit format family" });
    const aliases = within(dialog).getByRole("textbox", { name: "Extensions for Other" });
    await user.type(aliases, ".foo{Enter}");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      ".foo is reserved by Archive",
    );
    expect(within(dialog).queryByText(".foo", { exact: true })).not.toBeInTheDocument();
  });

  it("requires a saved revision before claiming a released built-in alias", async () => {
    const user = userEvent.setup();
    const office = customFormat("custom-office", "Office replacement", [".office"]);
    renderFamilyEditor({
      baselineBuiltinFormatOverrides: {
        docx: { extensions: [".docx", ".wordx"] },
      },
      builtinFormatOverrides: {
        docx: { extensions: [".docx"] },
      },
      customFormats: [office],
      formats: [format("docx", "Word DOCX", [".docx"])],
      initialFamily: customFamily("family-office", "Office files", ["custom-office"]),
    });

    const dialog = screen.getByRole("dialog", { name: "Edit format family" });
    const aliases = within(dialog).getByRole("textbox", {
      name: "Extensions for Office replacement",
    });
    await user.type(aliases, ".wordx{Enter}");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      ".wordx is reserved by Word DOCX",
    );
    await user.clear(aliases);
    await user.type(aliases, ".office2{Enter}");

    await user.click(within(dialog).getByRole("button", { name: "Register custom format" }));
    await user.click(within(dialog).getByRole("button", { name: "Show raw values" }));
    const rawSource = within(dialog).getByLabelText("Raw source (add / register)");
    await user.type(rawSource, ".wordx");
    await user.click(within(dialog).getByRole("button", { name: "Add or register groups" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      ".wordx is reserved by Word DOCX",
    );

    await user.clear(rawSource);
    await user.type(within(dialog).getByLabelText("Format name"), "Word replacement");
    await user.type(within(dialog).getByLabelText("Extension aliases"), ".wordx{Enter}");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      ".wordx is reserved by Word DOCX",
    );
    expect(within(dialog).getByRole("button", { name: "Register format" })).toBeDisabled();
  });

  it("blocks an inline alias rename from reusing another stable custom name", async () => {
    const user = userEvent.setup();
    const baselineFoo = customFormat("custom-a", "Foo", [".foo"]);
    const baselineBar = customFormat("custom-b", "Bar", [".bar"]);
    const renamedFoo = customFormat("custom-a", "Baz", [".foo"]);
    const currentBar = customFormat("custom-b", "Bar", [".bar"]);
    renderFamilyEditor({
      baselineCustomFormats: [baselineFoo, baselineBar],
      customFormats: [renamedFoo, currentBar],
      initialFamily: customFamily("family-bar", "Bar files", ["custom-b"]),
    });

    const dialog = screen.getByRole("dialog", { name: "Edit format family" });
    await user.click(within(dialog).getByRole("button", { name: "Edit alias group Bar" }));
    const editName = within(dialog).getByRole("textbox", { name: "Edit alias group name" });
    await user.clear(editName);
    await user.type(editName, "Foo{Enter}");

    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "Save that format's rename or deletion first, then reopen Settings",
    );
    await user.type(editName, "{Escape}");
    expect(within(dialog).getByRole("button", { name: "Edit alias group Bar" }))
      .toBeInTheDocument();
  });

  it("keeps family submission blocked until inline alias edits are valid and committed", async () => {
    const user = userEvent.setup();
    const alpha = customFormat("custom-alpha", "Alpha", [".alpha"]);
    const beta = customFormat("custom-beta", "Beta", [".beta"]);
    renderFamilyEditor({
      customFormats: [alpha, beta],
      initialFamily: customFamily("family-alpha", "Alpha files", ["custom-alpha"]),
    });

    const dialog = screen.getByRole("dialog", { name: "Edit format family" });
    const updateFamily = within(dialog).getByRole("button", { name: "Update family" });
    expect(updateFamily).toBeEnabled();

    await user.click(within(dialog).getByRole("button", { name: "Edit alias group Alpha" }));
    let editName = within(dialog).getByRole("textbox", { name: "Edit alias group name" });
    await user.clear(editName);
    await user.type(editName, "Beta{Enter}");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "A custom format named 'Beta' already exists",
    );
    expect(updateFamily).toBeDisabled();

    await user.clear(editName);
    await user.type(editName, "Alpha{Enter}");
    expect(within(dialog).getByRole("button", { name: "Edit alias group Alpha" }))
      .toBeInTheDocument();
    expect(updateFamily).toBeEnabled();

    await user.click(within(dialog).getByRole("button", {
      name: /Manage custom formats \(2\)/,
    }));
    await user.click(within(dialog).getByRole("button", { name: "Edit Beta" }));
    const betaName = within(dialog).getByRole("textbox", { name: "Format name" });
    await user.clear(betaName);
    await user.type(betaName, "Beta updated");
    await user.click(within(dialog).getByRole("button", { name: "Update format" }));
    await user.click(within(dialog).getByRole("button", {
      name: /Manage custom formats \(2\)/,
    }));
    const applyFormats = within(dialog).getByRole("button", { name: "Apply format changes" });
    expect(applyFormats).toBeEnabled();

    await user.click(within(dialog).getByRole("button", { name: "Edit alias group Alpha" }));
    editName = within(dialog).getByRole("textbox", { name: "Edit alias group name" });
    await user.clear(editName);
    await user.type(editName, "{Enter}");
    expect(updateFamily).toBeDisabled();
    expect(applyFormats).toBeDisabled();
    await user.type(editName, "Alpha{Enter}");
    expect(updateFamily).toBeEnabled();
    expect(applyFormats).toBeEnabled();

    const extensions = within(dialog).getByRole("textbox", { name: "Extensions for Alpha" });
    await user.type(extensions, ".exe{Enter}");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Blocked extension: .exe");
    expect(updateFamily).toBeDisabled();
    expect(applyFormats).toBeDisabled();
    await user.clear(extensions);
    await user.type(extensions, ".gamma{Enter}");
    expect(within(dialog).getByText(".gamma", { exact: true })).toBeInTheDocument();
    expect(updateFamily).toBeEnabled();
    expect(applyFormats).toBeEnabled();
  });

  it("submits every alias added while editing an existing custom format", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    const office = customFormat("custom-office", "Office", [".office"]);
    renderFamilyEditor({
      baselineCustomFormats: [office],
      customFormats: [office],
      initialFamily: customFamily("family-office", "Office files", ["custom-office"]),
      onSave,
    });

    const dialog = screen.getByRole("dialog", { name: "Edit format family" });
    await user.click(within(dialog).getByRole("button", {
      name: /Manage custom formats \(1\)/,
    }));
    await user.click(within(dialog).getByRole("button", { name: "Edit Office" }));
    let aliases = within(dialog).getByRole("textbox", { name: "Extension aliases" });
    await user.type(aliases, ".priv{Enter}");
    aliases = within(dialog).getByRole("textbox", { name: "Extension aliases" });
    await user.type(aliases, ".pkg{Enter}");
    await user.click(within(dialog).getByRole("button", { name: "Update format" }));
    await user.click(within(dialog).getByRole("button", { name: "Update family" }));

    expect(onSave).toHaveBeenCalledWith(
      expect.anything(),
      [expect.objectContaining({ extensions: [".office", ".priv", ".pkg"] })],
      expect.anything(),
    );
  });

  it("blocks raw and single-group registration from reusing a deleted stable name", async () => {
    const user = userEvent.setup();
    renderFamilyEditor({
      baselineCustomFormats: [customFormat("custom-a", "Foo", [".foo"])],
    });

    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(dialog).getByRole("button", { name: "Register custom format" }));
    await user.click(within(dialog).getByRole("button", { name: "Show raw values" }));
    const rawSource = within(dialog).getByLabelText("Raw source (add / register)");
    await user.click(rawSource);
    await user.paste("[Foo](.new)");
    await user.click(within(dialog).getByRole("button", { name: "Add or register groups" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "Save that format's rename or deletion first, then reopen Settings",
    );

    await user.clear(rawSource);
    await user.type(within(dialog).getByLabelText("Format name"), "Foo");
    await user.type(within(dialog).getByLabelText("Extension aliases"), ".new");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "Save that format's rename or deletion first, then reopen Settings",
    );
    expect(within(dialog).getByRole("button", { name: "Register format" })).toBeDisabled();
  });

  it("marks only preset members and restores editable preset aliases", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    renderFamilyEditor({
      formats: [format("docx", "Word DOCX", [".docx"])],
      onSave,
      presets: [preset("document", "Documents", ["docx"], {
        docx: [".docx"],
      })],
    });
    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(dialog).getByRole("radio", { name: "Preset" }));

    expect(dialog.querySelectorAll(".library-family-alias-preset")).toHaveLength(1);
    let aliases = within(dialog).getByRole("textbox", { name: "Extensions for Word DOCX" });
    await user.click(within(dialog).getByRole("button", {
      name: "Edit extension .docx",
    }));
    const editExtension = within(dialog).getByRole("textbox", {
      name: "Edit extension .docx",
    });
    await user.clear(editExtension);
    await user.type(editExtension, "wordx{Enter}");
    expect(within(dialog).getByText(".wordx", { exact: true })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", {
      name: "Restore preset defaults",
    }));
    expect(within(dialog).getByText(".docx", { exact: true })).toBeInTheDocument();

    aliases = within(dialog).getByRole("textbox", { name: "Extensions for Word DOCX" });
    await user.type(aliases, "wordx{Enter}");
    expect(within(dialog).getByText(".wordx", { exact: true })).toBeInTheDocument();
    expect(within(dialog).getByText("Modified from preset")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", {
      name: "Restore preset defaults",
    }));
    expect(within(dialog).queryByText(".wordx", { exact: true })).not.toBeInTheDocument();

    aliases = within(dialog).getByRole("textbox", { name: "Extensions for Word DOCX" });
    await user.type(aliases, "wordx{Enter}");
    await user.click(within(dialog).getByRole("button", { name: "Add family" }));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ format_ids: ["docx"] }),
      [],
      { docx: { extensions: [".docx", ".wordx"] } },
    );
  });

  it("keeps a copied preset restore atomic when another persisted format owns an alias", async () => {
    const user = userEvent.setup();
    const replacement = customFormat("custom-replacement", "Replacement", [".wordx"]);
    const savedFamily: LibraryPolicyFormatFamily = {
      family_id: "family-documents",
      name: "Documents",
      origin: "preset",
      preset_id: "document",
      preset_revision: 1,
      preset_baseline: {
        preset_id: "document",
        preset_revision: 1,
        format_ids: ["docx"],
        format_extensions: { docx: [".wordx"] },
      },
      format_ids: ["docx"],
      max_upload_bytes: null,
    };
    renderFamilyEditor({
      baselineBuiltinFormatOverrides: { docx: { extensions: [".docx"] } },
      baselineCustomFormats: [replacement],
      builtinFormatOverrides: { docx: { extensions: [".docx"] } },
      customFormats: [replacement],
      formats: [format("docx", "Word DOCX", [".wordx"])],
      initialFamily: savedFamily,
    });

    const dialog = screen.getByRole("dialog", { name: "Edit format family" });
    expect(within(dialog).getByText(".docx", { exact: true })).toBeInTheDocument();
    const updateFamily = within(dialog).getByRole("button", { name: "Update family" });
    expect(updateFamily).toBeEnabled();

    await user.click(within(dialog).getByRole("button", {
      name: "Restore preset defaults",
    }));

    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      ".wordx belonged to Replacement when this dialog opened",
    );
    expect(within(dialog).getByText(".docx", { exact: true })).toBeInTheDocument();
    expect(within(aliasGroupsList(dialog)).queryByText(".wordx", { exact: true }))
      .not.toBeInTheDocument();
    expect(updateFamily).toBeDisabled();
  });

  it("preserves package-declared alias ambiguity during explicit preset restore", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    const savedFamily: LibraryPolicyFormatFamily = {
      family_id: "family-hdf5",
      name: "HDF5 files",
      origin: "preset",
      preset_id: "hdf5",
      preset_revision: 1,
      preset_baseline: {
        preset_id: "hdf5",
        preset_revision: 1,
        format_ids: ["hdf5"],
        format_extensions: { hdf5: [".hdf"] },
      },
      format_ids: ["hdf5"],
      max_upload_bytes: null,
    };
    renderFamilyEditor({
      baselineBuiltinFormatOverrides: { hdf5: { extensions: [".hdf", ".h5"] } },
      builtinFormatOverrides: { hdf5: { extensions: [".hdf", ".h5"] } },
      formats: [
        format("hdf4", "HDF4", [".hdf"]),
        format("hdf5", "HDF5", [".hdf"]),
      ],
      initialFamily: savedFamily,
      onSave,
    });

    const dialog = screen.getByRole("dialog", { name: "Edit format family" });
    await user.click(within(dialog).getByRole("button", {
      name: "Remove extension .hdf",
    }));
    await user.type(
      within(dialog).getByRole("textbox", { name: "Extensions for HDF5" }),
      "hdf{Enter}",
    );
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(aliasGroupsList(dialog)).getByText(".hdf", { exact: true })).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", {
      name: "Restore preset defaults",
    }));
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(aliasGroupsList(dialog)).getByText(".hdf", { exact: true })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Update family" }));
    expect(onSave).toHaveBeenCalledWith(expect.anything(), [], {});
  });

  it("copies preset provenance without silently replacing Workspace alias overrides", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    renderFamilyEditor({
      baselineBuiltinFormatOverrides: { docx: { extensions: [".wordx"] } },
      builtinFormatOverrides: { docx: { extensions: [".wordx"] } },
      formats: [format("docx", "Word DOCX", [".docx"])],
      onSave,
      presets: [preset("document", "Documents", ["docx"], { docx: [".docx"] })],
    });

    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(dialog).getByRole("radio", { name: "Preset" }));
    expect(within(dialog).getByText(".wordx", { exact: true })).toBeInTheDocument();
    expect(within(dialog).getByText("Modified from preset")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Add family" }));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        format_ids: ["docx"],
        preset_baseline: expect.objectContaining({
          format_extensions: { docx: [".docx"] },
        }),
      }),
      [],
      { docx: { extensions: [".wordx"] } },
    );
  });

  it("discards dialog-local aliases when switching to another preset", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    renderFamilyEditor({
      formats: [
        format("docx", "Word DOCX", [".docx"]),
        format("xlsx", "Excel XLSX", [".xlsx"]),
      ],
      onSave,
      presets: [
        preset("document", "Documents", ["docx"], { docx: [".docx"] }),
        preset("sheet", "Spreadsheets", ["xlsx"], { xlsx: [".xlsx"] }),
      ],
    });
    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.click(within(dialog).getByRole("radio", { name: "Preset" }));
    await user.type(
      within(dialog).getByRole("textbox", { name: "Extensions for Word DOCX" }),
      "wordx{Enter}",
    );
    await user.selectOptions(within(dialog).getByLabelText("Family preset"), "sheet");
    await user.click(within(dialog).getByRole("button", { name: "Add family" }));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ format_ids: ["xlsx"] }),
      [],
      {},
    );
  });

  it("shows the deployment hard cap as the settable maximum, with an admin link", async () => {
    const user = userEvent.setup();
    const onOpenDeploymentSettings = vi.fn();
    renderFamilyEditor({
      formats: [{
        ...format("docx", "Word DOCX", [".docx"]),
        upload_limit_sources: { instance: 512_000_000 },
      } as unknown as LibraryFormatCapability],
      isServerAdmin: true,
      onOpenDeploymentSettings,
    });
    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.type(
      within(dialog).getByRole("combobox", { name: "Add formats" }),
      "docx{Enter}",
    );
    await user.click(within(dialog).getByRole("checkbox", {
      name: "Use workspace upload limit",
    }));

    expect(within(dialog).getByText(/512 MB is the most you can set here/))
      .toBeInTheDocument();
    const adminLink = within(dialog).getByRole("button", {
      name: "Raise it in deployment settings →",
    });
    await user.click(adminLink);
    expect(onOpenDeploymentSettings).toHaveBeenCalledOnce();
  });

  it("tells a non-admin that a server admin can raise the hard cap", async () => {
    const user = userEvent.setup();
    renderFamilyEditor({
      formats: [{
        ...format("docx", "Word DOCX", [".docx"]),
        upload_limit_sources: { instance: 512_000_000 },
      } as unknown as LibraryFormatCapability],
      isServerAdmin: false,
    });
    const dialog = screen.getByRole("dialog", { name: "Add format family" });
    await user.type(
      within(dialog).getByRole("combobox", { name: "Add formats" }),
      "docx{Enter}",
    );
    await user.click(within(dialog).getByRole("checkbox", {
      name: "Use workspace upload limit",
    }));

    expect(within(dialog).getByText(
      "A server admin can raise it in the server's deployment settings.",
    )).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", {
      name: "Raise it in deployment settings →",
    })).not.toBeInTheDocument();
  });
});

function renderFamilyEditor({
  baselineBuiltinFormatOverrides = {},
  baselineCustomFormats = [],
  builtinFormatOverrides = {},
  customFormats = [],
  families,
  formats = [],
  initialFamily = null,
  isServerAdmin = false,
  onOpenDeploymentSettings = vi.fn(),
  onSave = vi.fn(),
  presets = [],
}: {
  baselineBuiltinFormatOverrides?: LibraryPolicyBuiltinFormatOverrides;
  baselineCustomFormats?: LibraryPolicyCustomFormat[];
  builtinFormatOverrides?: LibraryPolicyBuiltinFormatOverrides;
  customFormats?: LibraryPolicyCustomFormat[];
  families?: LibraryPolicyFormatFamily[];
  formats?: LibraryFormatCapability[];
  initialFamily?: LibraryPolicyFormatFamily | null;
  isServerAdmin?: boolean;
  onOpenDeploymentSettings?: () => void;
  onSave?: (
    family: LibraryPolicyFormatFamily,
    customFormats: LibraryPolicyCustomFormat[],
    builtinFormatOverrides: LibraryPolicyBuiltinFormatOverrides,
  ) => void;
  presets?: LibraryFormatFamilyPreset[];
} = {}) {
  render(
    <FamilyEditorDialog
      baselineBuiltinFormatOverrides={baselineBuiltinFormatOverrides}
      baselineCustomFormats={baselineCustomFormats}
      builtinFormatOverrides={builtinFormatOverrides}
      busy={false}
      customFormats={customFormats}
      families={families ?? (initialFamily ? [initialFamily] : [])}
      formatOverrideIds={[]}
      formats={formats}
      initialFamily={initialFamily}
      isServerAdmin={isServerAdmin}
      onApplyCustomFormats={vi.fn()}
      onCancel={vi.fn()}
      onOpenDeploymentSettings={onOpenDeploymentSettings}
      onRevealConflict={vi.fn()}
      onSave={onSave}
      presets={presets}
      workspaceDefaultBytes={20_000_000}
    />,
  );
}

function aliasGroupsList(dialog: HTMLElement) {
  const list = dialog.querySelector(
    ".library-family-alias-groups, .library-family-alias-groups-empty",
  );
  if (!list) throw new Error("Format alias groups container not found");
  return list as HTMLElement;
}

function customFamily(
  familyId: string,
  name: string,
  formatIds: string[],
): LibraryPolicyFormatFamily {
  return {
    family_id: familyId,
    name,
    origin: "custom",
    preset_id: null,
    preset_revision: null,
    preset_baseline: null,
    format_ids: formatIds,
    max_upload_bytes: null,
  };
}

function customFormat(
  formatId: string,
  displayName: string,
  extensions: string[],
): LibraryPolicyCustomFormat {
  return { format_id: formatId, display_name: displayName, extensions };
}

function format(
  formatId: string,
  displayName: string,
  extensions: string[],
) {
  return {
    format_id: formatId,
    display_name: displayName,
    default_extensions: [...extensions],
    extensions: [...extensions],
    extensions_overridden: false,
    search_aliases: [],
    upload_limit_sources: {},
  } as unknown as LibraryFormatCapability;
}

function preset(
  presetId: string,
  label: string,
  formatIds: string[],
  formatExtensions: Record<string, string[]>,
): LibraryFormatFamilyPreset {
  return {
    preset_id: presetId,
    revision: 1,
    version_id: `${presetId}-v1`,
    label,
    description: label,
    format_ids: formatIds,
    format_extensions: formatExtensions,
    extensions: Object.values(formatExtensions).flat(),
  };
}
