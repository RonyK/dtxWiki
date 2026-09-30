import type {
  McpSetupArtifact,
  McpSetupPlan,
  McpShell,
  McpTemplateAtom,
} from "../../shared/api/mcpApps";
import {
  validateAppCommandArtifactSecurity,
  validateCommandArtifactSecurity,
  validateJsonSetupTemplate,
  validateSetupPlanCredentialContract,
} from "../../shared/api/mcpSetupSecurity";

/**
 * Which setup step an artifact belongs to. Registration and sign-in are
 * separate acts for every reviewed client, and collapsing them into one
 * numbered run of "setup commands" is what hid the login step.
 */
export type McpSetupStep = "register" | "authenticate";

export type RenderedMcpSetupArtifact = { step: McpSetupStep } & (
  | { kind: "command"; label: string; value: string }
  | { kind: "app-command"; label: string; value: string }
  | { kind: "copy-mcp-url" | "json-config"; label: string; value: string }
  | { kind: "deep-link"; label: string; value: string }
  | { kind: "instruction"; label: string; value: string }
);

type RenderContext = {
  accessToken?: string | null;
  locale: "en" | "ko";
  shell: McpShell;
  t?: (key: string, options: { defaultValue: string }) => string;
};

/**
 * Turns a reviewed symbolic plan into copyable text in the browser. This is a
 * renderer only: it never executes a command, opens a link, or persists the
 * one-time credential supplied by the caller.
 */
export function renderMcpSetupPlan(
  plan: McpSetupPlan,
  context: RenderContext,
): RenderedMcpSetupArtifact[] {
  if (plan.recipe_version !== 1) {
    throw new Error(`Unsupported MCP setup recipe version: ${plan.recipe_version}`);
  }
  validateSetupPlanCredentialContract(plan.auth, plan.artifacts);
  if (plan.auth === "access-key" && !context.accessToken) {
    throw new Error("An access key is required to render this setup plan");
  }
  if (plan.auth === "oauth" && context.accessToken) {
    throw new Error("OAuth setup cannot receive an access key");
  }
  if (context.accessToken && /[\r\n\0]/.test(context.accessToken)) {
    throw new Error("The access key contains unsafe control characters");
  }
  const slots = {
    accessToken: context.accessToken ?? null,
    mcpUrl: validateMcpUrl(plan.mcp_url),
    serverName: "dtxwiki",
  };
  // Ordinals count within a step, not across the whole recipe: "Login
  // command 2" is only meaningful among the sign-in commands beside it.
  const peerKey = (artifact: McpSetupArtifact) =>
    `${stepOf(artifact)}:${artifact.kind}`;
  const peerCounts = new Map<string, number>();
  for (const artifact of plan.artifacts) {
    const key = peerKey(artifact);
    peerCounts.set(key, (peerCounts.get(key) ?? 0) + 1);
  }
  const seen = new Map<string, number>();
  return plan.artifacts.map((artifact) => {
    const key = peerKey(artifact);
    const ordinal = (seen.get(key) ?? 0) + 1;
    seen.set(key, ordinal);
    return renderArtifact(
      artifact,
      ordinal,
      peerCounts.get(key) ?? 1,
      context,
      slots,
    );
  });
}

/**
 * An older server omits the field entirely, and an unknown value must not
 * silently drop an artifact out of the flow, so both resolve to the step that
 * always exists.
 */
function stepOf(artifact: McpSetupArtifact): McpSetupStep {
  return "step" in artifact && artifact.step === "authenticate"
    ? "authenticate"
    : "register";
}

function renderArtifact(
  artifact: McpSetupArtifact,
  ordinal: number,
  peerCount: number,
  context: RenderContext,
  slots: { accessToken: string | null; mcpUrl: string; serverName: string },
): RenderedMcpSetupArtifact {
  const step = stepOf(artifact);
  switch (artifact.kind) {
    case "command": {
      validateCommandArtifactSecurity(artifact);
      const executable = validateExecutable(artifact.commandName);
      const argv = [
        executable,
        ...artifact.argv.map((atoms) => renderAtoms(atoms, slots)),
      ];
      const command = argv.map((value) => shellQuote(value, context.shell)).join(" ");
      const environment = Object.entries(artifact.env ?? {}).map(
        ([name, atoms]) =>
          renderEnvironmentAssignment(
            validateEnvironmentName(name),
            renderAtoms(atoms, slots),
            context.shell,
          ),
      );
      return {
        kind: "command",
        label: commandLabel(step, "command", ordinal, peerCount, context),
        step,
        value: [...environment, command].join("\n"),
      };
    }
    case "app-command": {
      // Typed into the app itself, so it is deliberately not shell-quoted:
      // quoting would corrupt the text the user has to type.
      validateAppCommandArtifactSecurity(artifact);
      const value = renderAtoms(artifact.value, slots);
      // The artifact check above only sees atoms; the single-line slash
      // contract must also hold on the slot-substituted result. Shape, not
      // just prefix: "/bin/sh ..." is not a slash command.
      if (
        !/^\/[a-z][a-z0-9-]*(?:\s|$)/.test(value) ||
        /[\u0000-\u001f\u007f\u2028\u2029]/.test(value)
      ) {
        throw new Error(
          "MCP setup in-app command must render as a single slash-command line",
        );
      }
      return {
        kind: "app-command",
        label: commandLabel(
          step,
          "app-command",
          ordinal,
          peerCount,
          context,
        ),
        step,
        value,
      };
    }
    case "copy-mcp-url":
      return {
        kind: artifact.kind,
        label: localized(artifact.label, context.locale),
        step,
        value: renderAtoms(artifact.value, slots),
      };
    case "json-config":
      return {
        kind: artifact.kind,
        label: localized(artifact.label, context.locale),
        step,
        value: JSON.stringify(
          renderJsonTemplate(validateJsonSetupTemplate(artifact.value), slots),
          null,
          2,
        ),
      };
    case "deep-link": {
      const value = renderAtoms(artifact.url, slots);
      const parsed = new URL(value);
      if (parsed.protocol !== "https:") {
        throw new Error("MCP setup deep links must use HTTPS");
      }
      return {
        kind: artifact.kind,
        label: localized(artifact.label, context.locale),
        step,
        value,
      };
    }
    case "instruction": {
      const fallback = context.locale === "ko" ? "안내" : "Instruction";
      return {
        kind: artifact.kind,
        label:
          context.t?.("appHub.labelInstruction", { defaultValue: fallback }) ??
          fallback,
        step,
        value: localized(artifact.body, context.locale),
      };
    }
    default:
      return assertNever(artifact);
  }
}

/**
 * The label names the act, not the position in a list, so a user scanning the
 * page can tell registration from sign-in without reading the command.
 */
function commandLabel(
  step: McpSetupStep,
  kind: "command" | "app-command",
  ordinal: number,
  peerCount: number,
  context: RenderContext,
) {
  let base: string;
  if (step === "authenticate" && kind === "app-command") {
    const fallback =
      context.locale === "ko" ? "앱 내 로그인 명령" : "In-app login command";
    base =
      context.t?.("appHub.labelInAppLoginCommand", { defaultValue: fallback }) ??
      fallback;
  } else if (step === "authenticate") {
    const fallback = context.locale === "ko" ? "로그인 명령" : "Login command";
    base =
      context.t?.("appHub.labelLoginCommand", { defaultValue: fallback }) ?? fallback;
  } else if (kind === "app-command") {
    const fallback = context.locale === "ko" ? "앱 내 명령" : "In-app command";
    base =
      context.t?.("appHub.labelInAppCommand", { defaultValue: fallback }) ?? fallback;
  } else {
    const fallback = context.locale === "ko" ? "등록 명령" : "Register command";
    base =
      context.t?.("appHub.labelRegisterCommand", { defaultValue: fallback }) ??
      fallback;
  }
  return peerCount === 1 ? base : `${base} ${ordinal}`;
}

function renderAtoms(
  atoms: McpTemplateAtom[],
  slots: { accessToken: string | null; mcpUrl: string; serverName: string },
) {
  return atoms
    .map((atom) => {
      if (atom == null || typeof atom !== "object" || Array.isArray(atom)) {
        throw new Error("Invalid MCP setup template atom");
      }
      const keys = Object.keys(atom);
      if (keys.length !== 1) {
        throw new Error("MCP setup template atom contains extra properties");
      }
      if (
        keys[0] === "literal" &&
        "literal" in atom &&
        typeof atom.literal === "string"
      ) {
        return atom.literal;
      }
      if (!("slot" in atom) || typeof atom.slot !== "string") {
        throw new Error("Unknown MCP setup template atom");
      }
      if (atom.slot === "mcp-url") return slots.mcpUrl;
      if (atom.slot === "server-name") return slots.serverName;
      if (
        atom.slot === "access-token" ||
        atom.slot === "access-token-env" ||
        atom.slot === "authorization-bearer"
      ) {
        if (!slots.accessToken) {
          throw new Error("The setup plan contains an unresolved access key");
        }
        return atom.slot === "authorization-bearer"
          ? `Bearer ${slots.accessToken}`
          : slots.accessToken;
      }
      return assertNever(atom.slot);
    })
    .join("");
}

function renderJsonTemplate(
  value: unknown,
  slots: { accessToken: string | null; mcpUrl: string; serverName: string },
): unknown {
  if (
    value == null ||
    typeof value === "boolean" ||
    typeof value === "number" ||
    typeof value === "string"
  ) {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => renderJsonTemplate(item, slots));
  }
  if (typeof value !== "object") {
    throw new Error("Unsupported value in MCP JSON setup artifact");
  }
  const object = value as Record<string, unknown>;
  if (Object.keys(object).length === 1 && typeof object.literal === "string") {
    return object.literal;
  }
  if (Object.keys(object).length === 1 && typeof object.slot === "string") {
    return renderAtoms([{ slot: object.slot } as McpTemplateAtom], slots);
  }
  return Object.fromEntries(
    Object.entries(object).map(([key, item]) => [
      key,
      renderJsonTemplate(item, slots),
    ]),
  );
}

function renderEnvironmentAssignment(name: string, value: string, shell: McpShell) {
  if (shell === "powershell") {
    return `$env:${name} = '${value.replaceAll("'", "''")}'`;
  }
  return `export ${name}=${shellQuote(value, shell)}`;
}

function shellQuote(value: string, shell: McpShell) {
  if (/^[A-Za-z0-9_./:=@+-]+$/.test(value)) return value;
  if (shell === "powershell") return `'${value.replaceAll("'", "''")}'`;
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

function validateExecutable(value: string) {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value)) {
    throw new Error("Unsafe MCP setup executable");
  }
  return value;
}

function validateEnvironmentName(value: string) {
  if (!/^[A-Z_][A-Z0-9_]*$/.test(value)) {
    throw new Error("Unsafe MCP setup environment variable");
  }
  return value;
}

function validateMcpUrl(value: string) {
  // WHATWG URL parsing strips tab/CR/LF before validating, so the raw string
  // must be checked first or control characters survive into rendered slots.
  if (/[\u0000-\u001f\u007f\u2028\u2029]/.test(value)) {
    throw new Error("MCP setup URL contains unsafe control characters");
  }
  const parsed = new URL(value);
  const loopback =
    parsed.protocol === "http:" &&
    ["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname);
  if (parsed.protocol !== "https:" && !loopback) {
    throw new Error("MCP setup URL must use HTTPS or loopback HTTP");
  }
  if (!parsed.pathname.replace(/\/$/, "").endsWith("/mcp")) {
    throw new Error("MCP setup URL must point to /mcp");
  }
  return value.replace(/\/$/, "");
}

function localized(value: { en: string; ko: string }, locale: "en" | "ko") {
  return locale === "ko" ? value.ko : value.en;
}

function assertNever(value: never): never {
  throw new Error(`Unsupported MCP setup value: ${String(value)}`);
}
