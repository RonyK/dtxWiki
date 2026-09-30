import { describe, expect, it } from "vitest";

import {
  normalizeLibraryCapabilities,
  type LibraryCapabilities,
} from "../libraryFormatCapabilities";

const fallback: LibraryCapabilities = {
  policy_revision: "fallback-policy",
  capability_revision: "fallback-capability",
  max_files_per_batch: 1,
  formats: [],
};

describe("Library server catalog capabilities", () => {
  it("keeps server presets, configured families, and independent operation readiness", () => {
    const result = normalizeLibraryCapabilities({
      catalog_revision: "catalog-v1",
      policy_revision: "policy-v1",
      capability_revision: "capability-v1",
      policy_schema_version: 5,
      max_files_per_batch: 20,
      family_presets: [{
        preset_id: "video",
        revision: 1,
        version_id: "video-v1",
        label: "Video",
        description: "Common video formats",
        format_ids: ["mp4"],
        format_extensions: { mp4: [".mp4", ".m4v"] },
      }],
      families: [{
        family_id: "family-video",
        name: "Project video",
        origin: "preset",
        preset_id: "video",
        preset_revision: 1,
        preset_baseline: {
          preset_id: "video",
          preset_revision: 1,
          format_ids: ["mp4"],
          format_extensions: { mp4: [".mp4", ".m4v"] },
        },
        format_ids: ["mp4"],
        extensions: [".mp4", ".m4v"],
        max_upload_bytes: null,
        effective_max_upload_bytes: 20_000_000,
        uploadable_count: 1,
        preview_ready_count: 0,
        unavailable_count: 0,
        upload_limiting_source: "workspace",
        blockers: [],
      }],
      summary: {
        total: 1,
        uploadable: 1,
        preview_ready: 0,
        download_only: 1,
        unavailable: 0,
      },
      formats: [{
        format_id: "mp4",
        display_name: "MP4",
        category: "video",
        extensions: [".mp4", ".m4v"],
        default_extensions: [".mp4", ".m4v"],
        extensions_overridden: false,
        media_types: ["video/mp4"],
        search_aliases: ["mp4", "video/mp4"],
        provider_ids: ["core.detector.mp4"],
        upload: true,
        policy_enabled: true,
        deployment_available: true,
        converter_available: false,
        admission_state: "ready",
        storage_state: "available",
        conversion_state: "stored_unconverted",
        preview_state: "unavailable",
        render_state: "available",
        reason: "stored_without_converter",
        conversion: "stored_unconverted",
        preview_kind: "video",
        renderer_contract: "core.video.v1",
        max_bytes: 20_000_000,
        max_upload_bytes: 20_000_000,
        max_convertible_bytes: null,
        upload_limit_sources: { workspace: 20_000_000, family: 20_000_000 },
        upload_limiting_source: "family",
      }],
    }, fallback);

    expect(result.catalog_revision).toBe("catalog-v1");
    expect(result.policy_schema_version).toBe(5);
    expect(result.family_presets?.[0]).toMatchObject({
      preset_id: "video",
      format_extensions: { mp4: [".mp4", ".m4v"] },
      extensions: [".mp4", ".m4v"],
    });
    expect(result.families?.[0]).toMatchObject({
      family_id: "family-video",
      name: "Project video",
      preset_baseline: {
        preset_id: "video",
        preset_revision: 1,
        format_ids: ["mp4"],
        format_extensions: { mp4: [".mp4", ".m4v"] },
      },
      max_upload_bytes: null,
    });
    expect(result.formats[0]).toMatchObject({
      admission_state: "ready",
      conversion_state: "stored_unconverted",
      preview_state: "unavailable",
      render_state: "available",
      renderer_contract: "core.video.v1",
      default_extensions: [".mp4", ".m4v"],
      extensions_overridden: false,
      upload_limiting_source: "family",
    });
  });

  it("normalizes a minimal capability response without inventing upload permission", () => {
    const result = normalizeLibraryCapabilities({
      policy_revision: "legacy-policy",
      max_files_per_batch: 20,
      families: [{
        family_id: "text",
        label: "Text",
        mode: "inherit",
        format_ids: [],
      }],
      formats: [{
        format_id: "doc",
        category: "document",
        extensions: [".doc"],
        media_types: ["application/msword"],
        upload: false,
        conversion: "denied",
        preview_kind: "metadata",
        max_bytes: 2_000_000,
      }],
    }, fallback);

    expect(result.formats[0]).toMatchObject({
      upload: false,
      policy_enabled: false,
      admission_state: "ready",
      storage_state: "denied",
      conversion_state: "unavailable",
      preview_state: "unavailable",
    });
    expect(result.policy_schema_version).toBe(5);
    expect(result.families).toEqual([]);
    expect(result.family_presets).toEqual([]);
  });

  it("fails closed to the caller fallback for duplicate format identities", () => {
    const format = {
      format_id: "text",
      category: "text",
      extensions: [".txt"],
      media_types: ["text/plain"],
      upload: true,
      conversion: "available",
      preview_kind: "markdown",
      max_bytes: 2_000_000,
    };
    const result = normalizeLibraryCapabilities({
      policy_revision: "unsafe",
      max_files_per_batch: 20,
      formats: [format, format],
    }, fallback);

    expect(result).toEqual(fallback);
  });
});
