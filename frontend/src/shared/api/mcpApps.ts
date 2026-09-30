import { apiRequest } from "./client";
import {
  validateAppCommandArtifactSecurity,
  validateCommandArtifactSecurity,
  validateJsonSetupTemplate,
  validateSetupPlanCredentialContract,
} from "./mcpSetupSecurity";
import type { components } from "./types.gen";

type CatalogResponseDto = components["schemas"]["McpAppCatalogResponse"];
type SetupPlanResponseDto = components["schemas"]["McpSetupPlanResponse"];
type GeneratedSetupPlanRequest = components["schemas"]["McpSetupPlanRequest"];
type GeneratedMcpConnectionOption =
  components["schemas"]["McpConnectionOptionData"];
type GeneratedMcpCatalogApp = components["schemas"]["McpAppSummaryData"];
type GeneratedMcpGenericProfile =
  components["schemas"]["McpGenericProfileData"];
type GeneratedMcpAppCatalog = components["schemas"]["McpAppCatalogData"];

export type McpCompatibilityReasonCode =
  GeneratedMcpConnectionOption["reason_codes"][number];
export type McpCompatibilityDisplayReasonCode =
  | McpCompatibilityReasonCode
  | "unknown";
export type McpConnectionOption = Omit<
  GeneratedMcpConnectionOption,
  "reason_codes"
> & {
  reason_codes: McpCompatibilityDisplayReasonCode[];
};
export type McpAuthMethod = McpConnectionOption["auth"];
export type McpTransport = McpConnectionOption["transport"];
export type McpSurface = McpConnectionOption["surface"];
export type McpScopePreset = McpConnectionOption["default_scope_preset"];
export type McpCatalogApp = Omit<GeneratedMcpCatalogApp, "options"> & {
  options: McpConnectionOption[];
};
export type McpGenericProfile = Omit<GeneratedMcpGenericProfile, "options"> & {
  options: McpConnectionOption[];
};
export type McpAppCatalog = Omit<
  GeneratedMcpAppCatalog,
  "apps" | "generic_profiles"
> & {
  apps: McpCatalogApp[];
  generic_profiles: McpGenericProfile[];
};
export type McpPlatform = NonNullable<GeneratedSetupPlanRequest["platform"]>;
export type McpShell = NonNullable<GeneratedSetupPlanRequest["shell"]>;
export type McpEndpointReachability = NonNullable<
  GeneratedSetupPlanRequest["endpoint_reachability"]
>;
export type McpTemplateAtom =
  | components["schemas"]["LiteralAtom"]
  | components["schemas"]["McpUrlAtom"]
  | components["schemas"]["ServerNameAtom"]
  | components["schemas"]["AccessTokenAtom"];
type LocalizedText = components["schemas"]["LocalizedText"];
type NonSecretMcpTemplateAtom =
  | components["schemas"]["LiteralAtom"]
  | components["schemas"]["McpUrlAtom"]
  | components["schemas"]["ServerNameAtom"];
export type McpSetupPlan = components["schemas"]["McpSetupPlanData"];
export type McpSetupArtifact = McpSetupPlan["artifacts"][number];
export type McpSetupPlanRequest = Omit<GeneratedSetupPlanRequest, "locale"> & {
  // The backend defaults locale to English for older callers even though the
  // generated request schema currently marks it required.
  locale?: GeneratedSetupPlanRequest["locale"];
};

export type McpCatalogQuery = {
  locale?: "en" | "ko";
  platform?: McpPlatform;
  shell?: McpShell;
  appVersion?: string;
  endpointReachability?: McpEndpointReachability;
};

const MCP_COMPATIBILITY_REASON_CODE_MAP: Record<
  McpCompatibilityReasonCode,
  true
> = {
  catalog_app_unavailable: true,
  transport_unavailable: true,
  server_oauth_unavailable: true,
  endpoint_unreachable: true,
  platform_required: true,
  platform_unsupported: true,
  shell_required: true,
  shell_unsupported: true,
  app_version_required: true,
  app_version_too_old: true,
};
export const MCP_COMPATIBILITY_REASON_CODES = Object.freeze(
  Object.keys(MCP_COMPATIBILITY_REASON_CODE_MAP) as McpCompatibilityReasonCode[],
);
const MCP_COMPATIBILITY_REASON_CODE_SET = new Set<string>(
  MCP_COMPATIBILITY_REASON_CODES,
);

/** Unknown future codes are intentionally collapsed to a safe generic label. */
export function normalizeMcpCompatibilityReasonCode(
  value: string,
): McpCompatibilityDisplayReasonCode {
  return MCP_COMPATIBILITY_REASON_CODE_SET.has(value)
    ? (value as McpCompatibilityReasonCode)
    : "unknown";
}

/**
 * Generated DTOs define the transport boundary. The strict parser below still
 * fails closed at runtime so an unreviewed catalog artifact cannot reach the
 * renderer merely because a response was cast to its OpenAPI type.
 */
export async function getMcpAppCatalog(
  workspaceId: string,
  query: McpCatalogQuery = {},
  signal?: AbortSignal,
) {
  const params = new URLSearchParams();
  if (query.locale) params.set("locale", query.locale);
  if (query.platform) params.set("platform", query.platform);
  if (query.shell) params.set("shell", query.shell);
  if (query.appVersion) params.set("app_version", query.appVersion);
  if (query.endpointReachability) {
    params.set("endpoint_reachability", query.endpointReachability);
  }
  const suffix = params.size ? `?${params.toString()}` : "";
  const response = await apiRequest<CatalogResponseDto>(
    `/api/v1/workspaces/${workspaceId}/settings/mcp-apps${suffix}`,
    { signal },
  );
  return parseCatalog(response.data);
}

export async function createMcpAppSetupPlan(
  workspaceId: string,
  owner: { kind: "app" | "generic-profile"; id: string },
  request: McpSetupPlanRequest,
  signal?: AbortSignal,
) {
  const ownerPath =
    owner.kind === "app"
      ? `mcp-apps/${encodeURIComponent(owner.id)}`
      : `mcp-generic-profiles/${encodeURIComponent(owner.id)}`;
  const response = await apiRequest<SetupPlanResponseDto>(
    `/api/v1/workspaces/${workspaceId}/settings/${ownerPath}/setup-plan`,
    {
      method: "POST",
      body: JSON.stringify(request),
      signal,
    },
  );
  return parseSetupPlan(response.data);
}

function parseCatalog(value: unknown): McpAppCatalog {
  const catalog = objectValue(value, "MCP app catalog");
  if (catalog.schema_major !== 1 || typeof catalog.catalog_revision !== "string") {
    throw new Error("Unsupported MCP app catalog response");
  }
  const apps = arrayValue(catalog.apps, "catalog apps").map(parseApp);
  const genericProfiles = arrayValue(
    catalog.generic_profiles,
    "generic MCP profiles",
  ).map(parseGenericProfile);
  const capabilities = objectValue(
    catalog.server_capabilities,
    "MCP server capabilities",
  );
  return {
    schema_major: 1,
    catalog_revision: catalog.catalog_revision,
    apps,
    generic_profiles: genericProfiles,
    server_capabilities: {
      oauth_available: requiredBoolean(
        capabilities.oauth_available,
        "OAuth availability",
      ),
      oauth_unavailable_reason:
        typeof capabilities.oauth_unavailable_reason === "string"
          ? capabilities.oauth_unavailable_reason
          : null,
      oauth_dcr_enabled: requiredBoolean(
        capabilities.oauth_dcr_enabled,
        "OAuth dynamic client registration",
      ),
      mcp_url: requiredString(capabilities.mcp_url, "MCP endpoint URL"),
      transports: stringArray(capabilities.transports) as McpTransport[],
      endpoint_reachability: stringArray(
        capabilities.endpoint_reachability,
      ) as McpEndpointReachability[],
    },
  };
}

function parseApp(value: unknown): McpCatalogApp {
  const app = objectValue(value, "catalog app");
  const lifecycle = enumValue(app.lifecycle, ["active", "disabled"] as const);
  const verificationTier = enumValue(app.verification_tier, [
    "verified",
    "community",
  ] as const);
  return {
    id: requiredString(app.id, "app id"),
    label: requiredString(app.label, "app label"),
    aliases: stringArray(app.aliases),
    icon_url: requiredString(app.icon_url, "app icon URL"),
    vendor: nullableString(app.vendor),
    featured_rank:
      typeof app.featured_rank === "number" ? app.featured_rank : null,
    lifecycle,
    verification_tier: verificationTier,
    options: arrayValue(app.options, "app options").map(parseOption),
  };
}

function parseGenericProfile(value: unknown): McpGenericProfile {
  const profile = objectValue(value, "generic MCP profile");
  return {
    id: requiredString(profile.id, "profile id"),
    label: requiredString(profile.label, "profile label"),
    icon_url: requiredString(profile.icon_url, "profile icon URL"),
    options: arrayValue(profile.options, "profile options").map(parseOption),
  };
}

function parseOption(value: unknown): McpConnectionOption {
  const option = objectValue(value, "MCP connection option");
  return {
    variant_id: requiredString(option.variant_id, "variant id"),
    auth: enumValue(option.auth, ["oauth", "access-key"] as const),
    transport: enumValue(option.transport, ["streamable-http", "sse"] as const),
    surface: enumValue(option.surface, ["cli", "desktop", "ide", "web"] as const),
    default_scope_preset: enumValue(option.default_scope_preset, [
      "read-only",
      "agent-draft",
      "authoring",
    ] as const),
    available: requiredBoolean(option.available, "option availability"),
    reason_codes: stringArray(option.reason_codes).map(
      normalizeMcpCompatibilityReasonCode,
    ),
    priority: nonNegativeInteger(option.priority, "option priority"),
  };
}

function parseSetupPlan(value: unknown): McpSetupPlan {
  const plan = objectValue(value, "MCP setup plan");
  const artifacts = arrayValue(plan.artifacts, "setup artifacts").map(
    parseArtifact,
  );
  const recipeVersion = nonNegativeInteger(
    plan.recipe_version,
    "setup recipe version",
  );
  if (recipeVersion !== 1) {
    throw new Error(`Unsupported MCP setup recipe version: ${recipeVersion}`);
  }
  const parsed: McpSetupPlan = {
    catalog_revision: requiredString(plan.catalog_revision, "catalog revision"),
    owner_kind: enumValue(plan.owner_kind, ["app", "generic-profile"] as const),
    owner_id: requiredString(plan.owner_id, "setup owner id"),
    variant_id: requiredString(plan.variant_id, "setup variant id"),
    recipe_id: requiredString(plan.recipe_id, "setup recipe id"),
    recipe_version: recipeVersion,
    auth: enumValue(plan.auth, ["oauth", "access-key"] as const),
    transport: enumValue(plan.transport, ["streamable-http", "sse"] as const),
    surface: enumValue(plan.surface, ["cli", "desktop", "ide", "web"] as const),
    default_scope_preset: enumValue(plan.default_scope_preset, [
      "read-only",
      "agent-draft",
      "authoring",
    ] as const),
    mcp_url: requiredString(plan.mcp_url, "MCP URL"),
    artifacts,
    verification_instruction: requiredString(
      plan.verification_instruction,
      "verification instruction",
    ),
  };
  validateSetupPlanCredentialContract(parsed.auth, parsed.artifacts);
  return parsed;
}

function parseArtifact(value: unknown): McpSetupArtifact {
  const artifact = objectValue(value, "setup artifact");
  const kind = requiredString(artifact.kind, "artifact kind");
  const step = parseSetupStep(artifact.step);
  if (kind === "command") {
    const executableId = artifact.executableId ?? artifact.executable_id;
    const commandName = artifact.commandName ?? artifact.command_name;
    const env = artifact.env == null ? null : objectValue(artifact.env, "command env");
    const parsed: Extract<McpSetupArtifact, { kind: "command" }> = {
      kind,
      step,
      executableId: requiredString(executableId, "executable id"),
      commandName: requiredString(commandName, "command name"),
      argv: arrayValue(artifact.argv, "command argv").map((argument) =>
        parseAtoms(argument, "command argument"),
      ),
      env:
        env == null
          ? null
          : Object.fromEntries(
              Object.entries(env).map(([key, atoms]) => [
                key,
                parseAtoms(atoms, "environment value"),
              ]),
            ),
    };
    validateCommandArtifactSecurity(parsed);
    return parsed;
  }
  if (kind === "app-command") {
    const parsed: Extract<McpSetupArtifact, { kind: "app-command" }> = {
      kind,
      step,
      value: parseNonSecretAtoms(artifact.value, "in-app command"),
    };
    validateAppCommandArtifactSecurity(parsed);
    return parsed;
  }
  if (kind === "copy-mcp-url") {
    return {
      kind,
      step,
      label: parseLocalizedText(artifact.label),
      value: parseNonSecretAtoms(artifact.value, "MCP URL value"),
    };
  }
  if (kind === "json-config") {
    return {
      kind,
      step,
      label: parseLocalizedText(artifact.label),
      value: validateJsonSetupTemplate(artifact.value),
    };
  }
  if (kind === "deep-link") {
    return {
      kind,
      step,
      label: parseLocalizedText(artifact.label),
      url: parseNonSecretAtoms(artifact.url, "deep link URL"),
    };
  }
  if (kind === "instruction") {
    return { kind, step, body: parseLocalizedText(artifact.body) };
  }
  throw new Error(`Unsupported MCP setup artifact: ${kind}`);
}

/**
 * A missing step (older server) or an unrecognised one both resolve to
 * registration. Dropping the artifact instead would hide a step the server
 * considers part of the setup, which is the failure this field exists to fix.
 */
function parseSetupStep(value: unknown): "register" | "authenticate" {
  return value === "authenticate" ? "authenticate" : "register";
}

function parseAtoms(value: unknown, label: string): McpTemplateAtom[] {
  return arrayValue(value, label).map((candidate) => {
    const atom = objectValue(candidate, "setup template atom");
    if (typeof atom.literal === "string" && Object.keys(atom).length === 1) {
      return { literal: atom.literal };
    }
    if (
      typeof atom.slot === "string" &&
      [
        "mcp-url",
        "server-name",
        "access-token",
        "access-token-env",
        "authorization-bearer",
      ].includes(atom.slot) &&
      Object.keys(atom).length === 1
    ) {
      return { slot: atom.slot } as McpTemplateAtom;
    }
    throw new Error("Unsupported MCP setup template atom");
  });
}

function parseNonSecretAtoms(
  value: unknown,
  label: string,
): NonSecretMcpTemplateAtom[] {
  const atoms = parseAtoms(value, label);
  if (
    atoms.some(
      (atom) =>
        "slot" in atom &&
        (atom.slot === "access-token" ||
          atom.slot === "access-token-env" ||
          atom.slot === "authorization-bearer"),
    )
  ) {
    throw new Error(`Access keys are not allowed in ${label}`);
  }
  return atoms as NonSecretMcpTemplateAtom[];
}

function parseLocalizedText(value: unknown): LocalizedText {
  const text = objectValue(value, "localized setup text");
  return {
    en: requiredString(text.en, "English setup text"),
    ko: requiredString(text.ko, "Korean setup text"),
  };
}

function objectValue(value: unknown, label: string): Record<string, unknown> {
  if (value == null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Invalid ${label}`);
  }
  return value as Record<string, unknown>;
}

function arrayValue(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`Invalid ${label}`);
  return value;
}

function requiredString(value: unknown, label: string) {
  if (typeof value !== "string" || !value.trim()) throw new Error(`Invalid ${label}`);
  return value;
}

function nullableString(value: unknown) {
  return typeof value === "string" ? value : null;
}

function requiredBoolean(value: unknown, label: string) {
  if (typeof value !== "boolean") throw new Error(`Invalid ${label}`);
  return value;
}

function nonNegativeInteger(value: unknown, label: string) {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new Error(`Invalid ${label}`);
  }
  return value as number;
}

function stringArray(value: unknown) {
  return arrayValue(value, "string list").map((item) =>
    requiredString(item, "string list item"),
  );
}

function enumValue<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
): T[number] {
  if (typeof value !== "string" || !allowed.includes(value)) {
    throw new Error(`Unsupported MCP catalog value: ${String(value)}`);
  }
  return value as T[number];
}
