"use strict";

function isPlaintextEndpoint(server) {
  try {
    return new URL(server.url).protocol === "http:";
  } catch {
    return false;
  }
}

function authenticationPolicy(servers) {
  const selected = Array.isArray(servers) ? servers : [];
  const requiresPat = selected.some(isPlaintextEndpoint);
  const patAllowed = selected.length === 1;
  return Object.freeze({
    requiresPat,
    oauthAllowed: !requiresPat,
    patAllowed,
    preferredAuth: requiresPat && patAllowed ? "pat" : "oauth",
    blockedReason:
      requiresPat && !patAllowed
        ? "평문 loopback HTTP 서버는 PAT를 사용해 한 번에 하나씩 등록해야 합니다."
        : null,
  });
}

function registrationSummary(result) {
  const registrations = Array.isArray(result?.registrations) ? result.registrations : [];
  const wroteConfigCount = registrations.filter(
    (registration) => registration?.wrote_config === true,
  ).length;
  const failedRegistrationCount = registrations.length - wroteConfigCount;
  const secretEnvNames = [
    ...new Set(
      [
        ...(Array.isArray(result?.secret_env_names) ? result.secret_env_names : []),
        ...registrations.flatMap((registration) =>
          registration?.wrote_config === true && Array.isArray(registration.secret_env_names)
            ? registration.secret_env_names
            : [],
        ),
      ]
        .filter((name) => typeof name === "string")
        .map((name) => name.trim())
        .filter(Boolean),
    ),
  ].sort();
  const setupRequired = secretEnvNames.length > 0;
  if (result?.setup_required !== setupRequired) {
    throw new Error("MCP registration returned an inconsistent secret setup status.");
  }
  if (result?.config_writes_complete !== (failedRegistrationCount === 0)) {
    throw new Error("MCP registration returned an inconsistent config-write status.");
  }
  if (
    !Array.isArray(result?.configuration_failures) ||
    result.configuration_failures.length !== failedRegistrationCount
  ) {
    throw new Error("MCP registration returned incomplete config failure details.");
  }
  const configurationFailures = result.configuration_failures.map((failure) =>
    Object.freeze({
      client:
        typeof failure?.client === "string" && failure.client.trim()
          ? failure.client.trim()
          : "unknown client",
      configPath:
        typeof failure?.config_path === "string" && failure.config_path.trim()
          ? failure.config_path.trim()
          : null,
      reason:
        typeof failure?.reason === "string" && failure.reason.trim()
          ? failure.reason.trim()
          : "client configuration was not written",
    }),
  );
  let status = "complete";
  if (failedRegistrationCount > 0) {
    status = wroteConfigCount > 0 ? "partial-failure" : "failed";
  } else if (setupRequired) {
    status = "setup-required";
  }
  return Object.freeze({
    registrationCount: registrations.length,
    wroteConfigCount,
    setupRequired,
    secretEnvNames: Object.freeze(secretEnvNames),
    configurationFailures: Object.freeze(configurationFailures),
    status,
  });
}

const policy = Object.freeze({ authenticationPolicy, registrationSummary });

if (typeof module !== "undefined" && module.exports) {
  module.exports = policy;
}
if (typeof window !== "undefined") {
  window.dtxwikiMcpPolicy = policy;
}
