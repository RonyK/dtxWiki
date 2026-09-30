import { describe, expect, it } from "vitest";

import type { LibraryFormatCapability } from "../../../../shared/media/libraryFormatCapabilities";
import {
  createCustomFormat,
  customFormatOption,
  duplicateFamilyName,
  effectiveFormatUploadLimit,
  familyConflict,
  familyExtensions,
  familyFormats,
  familyFromPreset,
  formatForExtension,
  invalidCustomFormatExtensions,
  matchingFamilyFormats,
  normalizeCustomFormatExtensions,
  normalizeExtensionInput,
  parsePolicyBuiltinFormatOverrides,
  parsePolicyCustomFormats,
  parsePolicyFormatFamilies,
  serializePolicyBuiltinFormatOverrides,
  serializePolicyCustomFormats,
  serializePolicyFormatFamilies,
  unsafeLibraryFormatExtensions,
  type LibraryPolicyFormatFamily,
} from "../model";

const markdown = {
  format_id: "markdown",
  display_name: "Markdown",
  extensions: [".md", ".markdown"],
  search_aliases: ["md", "text/markdown"],
} as LibraryFormatCapability;

const spreadsheet = {
  format_id: "xlsx",
  display_name: "Excel workbook",
  extensions: [".xlsx"],
  search_aliases: ["excel", "spreadsheet"],
} as LibraryFormatCapability;

const textFamily: LibraryPolicyFormatFamily = {
  family_id: "family-text",
  name: "Text",
  origin: "custom",
  preset_id: null,
  preset_revision: null,
  preset_baseline: null,
  format_ids: ["markdown"],
  max_upload_bytes: null,
};

describe("Library format-family policy model", () => {
  it("defaults to no families and does not reinterpret legacy category projections", () => {
    expect(parsePolicyFormatFamilies(undefined)).toEqual([]);
    expect(parsePolicyFormatFamilies([{
      family_id: "text",
      label: "Text",
      mode: "inherit",
    }])).toEqual([]);
  });

  it("round-trips canonical configured families with inherited limits", () => {
    const parsed = parsePolicyFormatFamilies([{
      family_id: "family-text",
      name: " Text documents ",
      origin: "preset",
      preset_id: "text-documents",
      preset_revision: 2,
      format_ids: ["markdown", "markdown"],
      max_upload_bytes: null,
    }]);

    expect(serializePolicyFormatFamilies(parsed)).toEqual([{
      family_id: "family-text",
      name: "Text documents",
      origin: "preset",
      preset_id: "text-documents",
      preset_revision: 2,
      preset_baseline: {
        preset_id: "text-documents",
        preset_revision: 2,
        format_ids: ["markdown"],
        format_extensions: {},
      },
      format_ids: ["markdown"],
      max_upload_bytes: null,
    }]);
  });

  it("resolves an extension alias to one canonical format and projects every alias", () => {
    const formats = [markdown, spreadsheet];

    expect(normalizeExtensionInput(" MARKDOWN ")).toBe(".markdown");
    expect(formatForExtension("markdown", formats)?.format_id).toBe("markdown");
    expect(matchingFamilyFormats("excel", formats, [])).toEqual([spreadsheet]);
    expect(familyExtensions(textFamily, formats)).toEqual([".md", ".markdown"]);
  });

  it("reports the other family for canonical format and case-insensitive name conflicts", () => {
    expect(familyConflict("markdown", [textFamily], null)).toEqual({
      familyId: "family-text",
      familyName: "Text",
      formatId: "markdown",
    });
    expect(duplicateFamilyName(" text ", [textFamily], null)).toBe(textFamily);
    expect(familyConflict("markdown", [textFamily], "family-text")).toBeNull();
  });

  it("creates a revision-pinned preset snapshot that inherits the Workspace limit", () => {
    expect(familyFromPreset({
      preset_id: "office",
      revision: 3,
      version_id: "office-v3",
      label: "Office",
      description: "Office documents",
      format_ids: ["xlsx"],
      format_extensions: { xlsx: [".xlsx"] },
      extensions: [".xlsx"],
    })).toMatchObject({
      family_id: expect.stringMatching(/^family-/),
      name: "Office",
      origin: "preset",
      preset_id: "office",
      preset_revision: 3,
      preset_baseline: {
        preset_id: "office",
        preset_revision: 3,
        format_ids: ["xlsx"],
        format_extensions: { xlsx: [".xlsx"] },
      },
      format_ids: ["xlsx"],
      max_upload_bytes: null,
    });
  });

  it("normalizes a Workspace custom format with several extension aliases", () => {
    const parsed = parsePolicyCustomFormats([{
      format_id: "CUSTOM-REPORT",
      display_name: "  Report   bundle ",
      extensions: ["REPORT", ".rpt", ".report"],
    }]);
    expect(serializePolicyCustomFormats(parsed)).toEqual([{
      format_id: "custom-report",
      display_name: "Report bundle",
      extensions: [".report", ".rpt"],
    }]);
    expect(customFormatOption(parsed[0]).search_aliases).toContain("report");
    expect(normalizeCustomFormatExtensions("foo, .BAR foo")).toEqual([".foo", ".bar"]);
    expect(invalidCustomFormatExtensions([".safe", ".not.safe"])).toEqual([".not.safe"]);
    expect(createCustomFormat("Private data", [".private"]).format_id)
      .toMatch(/^custom-[a-z0-9][a-z0-9_-]{0,55}$/);
  });

  it("returns family formats in persisted format_ids order", () => {
    const formats = [markdown, spreadsheet];
    const first = formats[0];
    const second = formats[1];
    expect(familyFormats({ format_ids: [second.format_id, first.format_id] }, formats))
      .toEqual([second, first]);
  });

  it("round-trips safe built-in aliases and rejects unsafe alias ownership", () => {
    const parsed = parsePolicyBuiltinFormatOverrides({
      markdown: { extensions: ["MD", ".markdown"] },
      "custom-report": { extensions: [".report"] },
      archive: { extensions: [".zip"] },
    });

    expect(serializePolicyBuiltinFormatOverrides(parsed)).toEqual({
      markdown: { extensions: [".md", ".markdown"] },
    });
    expect(unsafeLibraryFormatExtensions([".safe", ".zip", ".ps1"]))
      .toEqual([".zip", ".ps1"]);
    expect(parsePolicyCustomFormats([{
      format_id: "custom-archive",
      display_name: "Archive",
      extensions: [".zip"],
    }])).toEqual([]);
  });

  it("applies exact then family then Workspace specificity before the hard cap", () => {
    const format = {
      ...spreadsheet,
      upload_limit_sources: {
        workspace: 20_000_000,
        instance: 100_000_000,
      },
    } as LibraryFormatCapability;

    expect(effectiveFormatUploadLimit(format, {
      workspaceMaxBytes: 18_000_000,
      familyMaxBytes: 75_000_000,
      formatMaxBytes: 80_000_000,
    })).toEqual({ bytes: 80_000_000, source: "format" });
    expect(effectiveFormatUploadLimit(format, {
      workspaceMaxBytes: 18_000_000,
      familyMaxBytes: 75_000_000,
      formatMaxBytes: null,
    })).toEqual({ bytes: 75_000_000, source: "family" });
    expect(effectiveFormatUploadLimit({
      ...format,
      upload_limit_sources: { instance: 30_000_000 },
    }, {
      workspaceMaxBytes: 18_000_000,
      familyMaxBytes: 25_000_000,
      formatMaxBytes: 40_000_000,
    })).toEqual({ bytes: 30_000_000, source: "instance" });
  });
});
