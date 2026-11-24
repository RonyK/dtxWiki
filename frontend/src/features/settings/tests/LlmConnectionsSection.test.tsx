import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import { LlmConnectionsSection } from "../LlmConnectionsSection";

beforeEach(() => {
  vi.restoreAllMocks();
  setLocale("en", { persist: false });
});

it("retries only the key replacement after the settings were saved", async () => {
  // The server echoes profiles with explicit nulls; once the rename is saved,
  // a retry must not look like a profile change (which would reset the
  // verified setup) and must use the revision the rename returned.
  const connection = {
    id: "c1",
    name: "Team",
    adapter_id: "builtin.anthropic.messages",
    adapter_implementation_version: "1.0.0",
    config_schema_version: 1,
    api_profile: "builtin.anthropic.messages",
    endpoint_profile_id: null,
    provider_options: {},
    model_profiles: [
      {
        id: "m",
        model_id: "claude-model",
        display_name: null,
        context_window: null,
        max_output_tokens: null,
        options: [],
      },
    ],
    status: "enabled",
    verification_status: "ready",
    verified_at: "2026-11-24T00:00:00Z",
    credential_updated_at: "2026-11-24T00:00:00Z",
    has_credential: true,
    revision: 4,
    created_at: "2026-11-24T00:00:00Z",
    updated_at: "2026-11-24T00:00:00Z",
    verification: null,
  };
  const adapter = {
    adapter_id: "builtin.anthropic.messages",
    adapter_version: "1.0.0",
    display_name: { en: "Anthropic Messages API" },
    config_schema: { type: "object", properties: {} },
    credential_schema: {
      type: "object",
      properties: { api_key: { type: "string", writeOnly: true } },
      required: ["api_key"],
    },
    options: ["temperature"],
    endpoints: [{ id: null, label: null, base_url: "https://api.anthropic.com/v1" }],
  };
  const writes: Array<[string, unknown]> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input.toString();
    if (init?.method && init.method !== "GET") {
      const body = JSON.parse(String(init.body));
      writes.push([`${init.method} ${url.replace(/^.*\/api\/v1/, "")}`, body]);
      if (init.method === "PATCH") {
        return json({ data: { ...connection, name: body.name, revision: 5 } });
      }
      if (writes.length === 2) {
        return json({ error_code: "internal_error", message: "boom" }, 500);
      }
      return json({
        data: {
          connection: { ...connection, revision: 6 },
          verification: { status: "succeeded", error_code: null, results: [] },
        },
      });
    }
    if (url.endsWith("/api/v1/llm-adapters")) return json({ data: [adapter] });
    if (url.endsWith("/api/v1/me/llm-connections")) return json({ data: [connection] });
    throw new Error(`Unexpected request: ${url}`);
  });
  const user = userEvent.setup();
  render(<LlmConnectionsSection labelledBy="heading" scope={{ kind: "user" }} />);
  await user.click(await screen.findByRole("button", { name: "Edit" }));
  const dialog = screen.getByRole("dialog");
  await user.clear(within(dialog).getByLabelText("Name"));
  await user.type(within(dialog).getByLabelText("Name"), "Renamed");
  await user.type(within(dialog).getByLabelText("API key"), "sk-new");
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  await vi.waitFor(() => expect(writes).toHaveLength(2));
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  await vi.waitFor(() => expect(writes).toHaveLength(3));
  const keyReplacement = (revision: number) => [
    "PUT /me/llm-connections/c1/credential",
    { expected_revision: revision, credential: { api_key: "sk-new" } },
  ];
  expect(writes).toEqual([
    ["PATCH /me/llm-connections/c1", expect.objectContaining({ expected_revision: 4, name: "Renamed" })],
    keyReplacement(5),
    keyReplacement(5),
  ]);
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
