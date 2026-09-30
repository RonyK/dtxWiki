import type { McpSetupArtifact } from "./mcpApps";

type CommandArtifact = Extract<McpSetupArtifact, { kind: "command" }>;
type AppCommandArtifact = Extract<McpSetupArtifact, { kind: "app-command" }>;
type ClosedSlot =
  | "mcp-url"
  | "server-name"
  | "access-token"
  | "access-token-env"
  | "authorization-bearer";
type ClosedAtom =
  | { literal: string }
  | { slot: ClosedSlot };

const AUTHORIZATION_BEARER_PREFIX = "Authorization: Bearer ";
const SAFE_CATALOG_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const SAFE_EXECUTABLE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const SAFE_ENVIRONMENT_NAME = /^[A-Z][A-Z0-9_]{1,63}$/;
const PUBLIC_SLOTS = new Set(["mcp-url", "server-name"]);
const SECRET_SLOTS = new Set([
  "access-token",
  "access-token-env",
  "authorization-bearer",
]);
const FORBIDDEN_EXECUTABLES = new Set([
  "bash",
  "cmd",
  "cmd.exe",
  "powershell",
  "powershell.exe",
  "pwsh",
  "sh",
  "zsh",
]);
const FORBIDDEN_COMMAND_STRING_ARGUMENTS = new Set([
  "-c",
  "/c",
  "-command",
  "--command",
]);

/**
 * Mirrors the backend's closed command secret-sink policy without branching on
 * an app or executable ID. New sinks therefore require a reviewed code change.
 */
export function validateCommandArtifactSecurity(artifact: CommandArtifact) {
  if (
    typeof artifact.executableId !== "string" ||
    !SAFE_CATALOG_ID.test(artifact.executableId)
  ) {
    throw new Error("Invalid MCP setup executable ID");
  }
  if (
    typeof artifact.commandName !== "string" ||
    !SAFE_EXECUTABLE_NAME.test(artifact.commandName)
  ) {
    throw new Error("Unsafe MCP setup executable");
  }
  if (FORBIDDEN_EXECUTABLES.has(artifact.commandName.toLowerCase())) {
    throw new Error("Shell interpreter is not an approved MCP setup executable");
  }
  if (!Array.isArray(artifact.argv) || artifact.argv.length === 0) {
    throw new Error("Invalid MCP setup command arguments");
  }
  for (const argument of artifact.argv) {
    const atoms = closedAtoms(argument, "command argument");
    if (atoms.length === 0) {
      throw new Error("Invalid MCP setup command arguments");
    }
    const renderedLiterals = atoms
      .filter((atom): atom is Extract<ClosedAtom, { literal: string }> =>
        "literal" in atom,
      )
      .map((atom) => atom.literal)
      .join("")
      .toLowerCase();
    if (FORBIDDEN_COMMAND_STRING_ARGUMENTS.has(renderedLiterals)) {
      throw new Error("MCP setup command-string mode is not allowed");
    }
    if (!containsSecret(atoms)) continue;
    const approved =
      atoms.length === 2 &&
      "literal" in atoms[0] &&
      atoms[0].literal === AUTHORIZATION_BEARER_PREFIX &&
      "slot" in atoms[1] &&
      atoms[1].slot === "access-token";
    if (!approved) {
      throw new Error("Access key uses an unapproved command sink");
    }
  }

  if (artifact.env == null) return;
  if (typeof artifact.env !== "object" || Array.isArray(artifact.env)) {
    throw new Error("Invalid MCP setup command environment");
  }
  for (const [name, value] of Object.entries(artifact.env)) {
    const atoms = closedAtoms(value, "command environment value");
    if (!containsSecret(atoms)) continue;
    const approved =
      SAFE_ENVIRONMENT_NAME.test(name) &&
      atoms.length === 1 &&
      "slot" in atoms[0] &&
      atoms[0].slot === "access-token-env";
    if (!approved) {
      throw new Error("Access key uses an unapproved command sink");
    }
  }
}

/**
 * An in-app command is typed into the client prompt, so it never passes
 * through a shell and is never an approved credential sink. Pinning it to a
 * single-line slash command keeps this kind from becoming a way to hand the
 * user a shell invocation that skipped the executable checks above.
 */
export function validateAppCommandArtifactSecurity(
  artifact: AppCommandArtifact,
) {
  const atoms = closedAtoms(artifact.value, "in-app command");
  if (atoms.length === 0) {
    throw new Error("Invalid MCP setup in-app command");
  }
  if (containsSecret(atoms)) {
    throw new Error("Access key uses an unapproved in-app command sink");
  }
  const first = atoms[0];
  // Shape, not just prefix: "/bin/sh -c ..." starts with "/" but is a shell
  // invocation, not a client slash command.
  if (
    !("literal" in first) ||
    !/^\/[a-z][a-z0-9-]*(?:\s|$)/.test(first.literal)
  ) {
    throw new Error("MCP setup in-app command must be a slash command");
  }
  for (const atom of atoms) {
    if (!("literal" in atom)) continue;
    if (/[\r\n\0]/.test(atom.literal)) {
      throw new Error("MCP setup in-app command must be a single line");
    }
    if (/[\u0000-\u001f\u007f\u2028\u2029]/.test(atom.literal)) {
      throw new Error(
        "MCP setup in-app command must not contain control characters",
      );
    }
  }
}

/**
 * Parses the recursive JSON-template vocabulary into a fresh object. A bearer
 * credential is accepted only as `{slot: "access-token"}` at the root
 * `bearerToken` property or `{slot: "authorization-bearer"}` at the exact
 * `headers.Authorization` path; alternate sinks fail.
 */
export function validateJsonSetupTemplate(value: unknown): unknown {
  return validateJsonValue(value, []);
}

/**
 * Couples the recipe auth mode to its reviewed credential sinks. Artifact
 * validation remains app-ID independent, while this plan-level check prevents
 * an incomplete access-key recipe or a drifted OAuth recipe from reaching the
 * renderer.
 */
export function validateSetupPlanCredentialContract(
  auth: "oauth" | "access-key",
  artifacts: McpSetupArtifact[],
) {
  let approvedSecretSinks = 0;

  for (const artifact of artifacts) {
    switch (artifact.kind) {
      case "command":
        validateCommandArtifactSecurity(artifact);
        approvedSecretSinks += countCommandSecretSinks(artifact);
        break;
      case "app-command":
        validateAppCommandArtifactSecurity(artifact);
        break;
      case "json-config": {
        const template = validateJsonSetupTemplate(artifact.value);
        approvedSecretSinks += countJsonSecretSinks(template);
        break;
      }
      case "copy-mcp-url":
        rejectSecretAtoms(artifact.value, "MCP URL artifact");
        break;
      case "deep-link":
        rejectSecretAtoms(artifact.url, "deep-link artifact");
        break;
      case "instruction":
        break;
    }
  }

  if (auth === "access-key" && approvedSecretSinks === 0) {
    throw new Error(
      "Access-key setup plan must include an approved credential sink",
    );
  }
  if (auth === "oauth" && approvedSecretSinks !== 0) {
    throw new Error("OAuth setup plan must not include a credential sink");
  }
}

function validateJsonValue(value: unknown, path: string[]): unknown {
  if (
    value == null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("MCP JSON setup contains a non-finite number");
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item, index) =>
      validateJsonValue(item, [...path, String(index)]),
    );
  }
  if (typeof value !== "object") {
    throw new Error("MCP JSON setup contains a non-JSON value");
  }

  const object = value as Record<string, unknown>;
  const keys = Object.keys(object);
  if (keys.length === 1 && keys[0] === "literal") {
    if (typeof object.literal !== "string") {
      throw new Error("Invalid MCP JSON literal atom");
    }
    return { literal: object.literal };
  }
  if (keys.length === 1 && keys[0] === "slot") {
    if (typeof object.slot !== "string") {
      throw new Error("Invalid MCP JSON slot atom");
    }
    if (PUBLIC_SLOTS.has(object.slot)) {
      return { slot: object.slot };
    }
    if (
      object.slot === "access-token" &&
      path.length === 1 &&
      path[0] === "bearerToken"
    ) {
      return { slot: object.slot };
    }
    if (
      object.slot === "authorization-bearer" &&
      path.length === 2 &&
      path[0] === "headers" &&
      path[1] === "Authorization"
    ) {
      return { slot: object.slot };
    }
    if (SECRET_SLOTS.has(object.slot)) {
      throw new Error("Access key uses an unapproved JSON sink");
    }
    throw new Error("Unknown MCP JSON setup slot");
  }
  if (keys.includes("literal") || keys.includes("slot")) {
    throw new Error("MCP JSON setup atom contains extra properties");
  }
  if (path.length > 0 && keys.includes("bearerToken")) {
    throw new Error("Nested bearerToken is not an approved JSON field");
  }

  return Object.fromEntries(
    keys.map((key) => [
      key,
      validateJsonValue(object[key], [...path, key]),
    ]),
  );
}

function closedAtoms(value: unknown, label: string): ClosedAtom[] {
  if (!Array.isArray(value)) throw new Error(`Invalid MCP setup ${label}`);
  return value.map((candidate) => closedAtom(candidate, label));
}

function closedAtom(value: unknown, label: string): ClosedAtom {
  if (value == null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Invalid MCP setup ${label} atom`);
  }
  const atom = value as Record<string, unknown>;
  const keys = Object.keys(atom);
  if (keys.length !== 1) {
    throw new Error(`MCP setup ${label} atom contains extra properties`);
  }
  if (keys[0] === "literal" && typeof atom.literal === "string") {
    return { literal: atom.literal };
  }
  if (
    keys[0] === "slot" &&
    typeof atom.slot === "string" &&
    (PUBLIC_SLOTS.has(atom.slot) || SECRET_SLOTS.has(atom.slot))
  ) {
    return { slot: atom.slot as ClosedSlot };
  }
  throw new Error(`Unknown MCP setup ${label} atom`);
}

function containsSecret(atoms: ClosedAtom[]) {
  return atoms.some((atom) => "slot" in atom && SECRET_SLOTS.has(atom.slot));
}

function countCommandSecretSinks(artifact: CommandArtifact) {
  const argvCount = artifact.argv.reduce(
    (count, argument) =>
      count +
      closedAtoms(argument, "command argument").filter(
        (atom) => "slot" in atom && SECRET_SLOTS.has(atom.slot),
      ).length,
    0,
  );
  const environmentCount = Object.values(artifact.env ?? {}).reduce(
    (count, value) =>
      count +
      closedAtoms(value, "command environment value").filter(
        (atom) => "slot" in atom && SECRET_SLOTS.has(atom.slot),
      ).length,
    0,
  );
  return argvCount + environmentCount;
}

function countJsonSecretSinks(value: unknown): number {
  if (Array.isArray(value)) {
    return value.reduce(
      (count, item) => count + countJsonSecretSinks(item),
      0,
    );
  }
  if (value == null || typeof value !== "object") return 0;
  const object = value as Record<string, unknown>;
  if (
    Object.keys(object).length === 1 &&
    typeof object.slot === "string" && SECRET_SLOTS.has(object.slot)
  ) {
    return 1;
  }
  return Object.values(object).reduce<number>(
    (count, item) => count + countJsonSecretSinks(item),
    0,
  );
}

function rejectSecretAtoms(value: unknown, label: string) {
  const atoms = closedAtoms(value, label);
  if (containsSecret(atoms)) {
    throw new Error(`Access key uses an unapproved ${label} sink`);
  }
}
