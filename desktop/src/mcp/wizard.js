"use strict";

const api = window.dtxwikiDesktop;
const { authenticationPolicy, registrationSummary } = window.dtxwikiMcpPolicy;
const labels = {
  "claude-code": "Claude Code",
  codex: "Codex CLI",
  "gemini-cli": "Gemini CLI",
  antigravity: "Google Antigravity",
};

const form = document.querySelector("#wizard-form");
const serverList = document.querySelector("#server-list");
const clientList = document.querySelector("#client-list");
const tokenRow = document.querySelector("#token-row");
const tokenInput = document.querySelector("#access-token");
const authPolicyHint = document.querySelector("#auth-policy");
const errorBox = document.querySelector("#error");
const resultBox = document.querySelector("#result");
let serversById = new Map();

function selectedServerIds() {
  return [...document.querySelectorAll('input[name="server"]:checked')].map((item) => item.value);
}

function selectedServers() {
  return selectedServerIds().map((serverId) => serversById.get(serverId)).filter(Boolean);
}

function updateCapabilities() {
  const oauthInput = document.querySelector('input[name="auth"][value="oauth"]');
  const patInput = document.querySelector('input[name="auth"][value="pat"]');
  const policy = authenticationPolicy(selectedServers());

  oauthInput.disabled = !policy.oauthAllowed;
  patInput.disabled = !policy.patAllowed;
  if (policy.preferredAuth === "pat" && policy.patAllowed) {
    patInput.checked = true;
  } else if (!patInput.checked || !policy.patAllowed) {
    oauthInput.checked = policy.oauthAllowed;
    patInput.checked = false;
  }

  const patSelected = patInput.checked && policy.patAllowed;
  tokenRow.hidden = !patSelected;
  tokenInput.required = patSelected;
  authPolicyHint.hidden = !policy.requiresPat;
  authPolicyHint.textContent =
    policy.blockedReason ??
    "평문 loopback HTTP MCP 연결에는 OAuth를 사용할 수 없어 PAT가 자동 선택되었습니다.";
}

function option(input, title, detail) {
  const label = document.createElement("label");
  label.className = "option compact";
  const copy = document.createElement("span");
  const strong = document.createElement("strong");
  strong.textContent = title;
  const small = document.createElement("small");
  small.textContent = detail;
  copy.append(strong, small);
  label.append(input, copy);
  return label;
}

function render(current) {
  serversById = new Map(current.remoteServers.map((server) => [server.id, server]));
  serverList.replaceChildren(
    ...current.remoteServers.map((server) => {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "server";
      input.value = server.id;
      input.addEventListener("change", updateCapabilities);
      return option(input, server.name, `${server.url} · ${server.mcpName}`);
    }),
  );
  document.querySelector("#no-servers").hidden = current.remoteServers.length > 0;
  clientList.replaceChildren(
    ...current.clients.map((client) => {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "client";
      input.value = client.id;
      input.checked = client.detected;
      const status = client.detected ? `설치 감지됨 · ${client.configPath}` : "설치 경로가 아직 감지되지 않음";
      return option(input, labels[client.id] || client.id, status);
    }),
  );
  updateCapabilities();
}

function busy(value) {
  for (const element of form.querySelectorAll("button, input")) element.disabled = value;
}

function secretSetupLines(summary) {
  if (!summary.setupRequired) return [];
  return [
    "반영된 설정에도 추가 인증 설정이 필요합니다.",
    `AI 클라이언트를 시작하기 전에 다음 환경 변수에 방금 입력한 PAT를 설정하세요: ${summary.secretEnvNames.join(", ")}`,
    "dtxWiki Desktop은 PAT를 시스템 또는 전역 환경 변수에 저장하지 않습니다. 환경 변수를 설정한 뒤 AI 클라이언트를 다시 시작하세요.",
  ];
}

function renderRegistrationResult(summary) {
  resultBox.classList.toggle("setup-required", summary.status === "setup-required");
  resultBox.classList.toggle(
    "partial-failure",
    summary.status === "partial-failure" || summary.status === "failed",
  );
  resultBox.dataset.status = summary.status;

  if (summary.configurationFailures.length > 0) {
    const heading =
      summary.status === "partial-failure" ? "일부 MCP 설정 실패" : "MCP 설정 실패";
    const failureLines = summary.configurationFailures.map((failure) => {
      const client = labels[failure.client] || failure.client;
      const location = failure.configPath ? ` (${failure.configPath})` : "";
      return `- ${client}${location}: ${failure.reason}`;
    });
    resultBox.textContent = [
      heading,
      `설정 파일 반영: ${summary.wroteConfigCount}/${summary.registrationCount}개`,
      ...failureLines,
      "이미 반영된 다른 클라이언트 설정은 유지되었습니다. 위 문제를 해결한 뒤 실패한 등록을 다시 실행하세요.",
      ...secretSetupLines(summary),
    ].join("\n");
    return;
  }

  resultBox.textContent = summary.setupRequired
    ? [
        `MCP 설정 파일 반영: ${summary.wroteConfigCount}개`,
        "추가 인증 설정 필요",
        ...secretSetupLines(summary).slice(1),
      ].join("\n")
    : `등록 완료: ${summary.wroteConfigCount}개의 HTTP MCP 설정이 반영되었습니다. AI 클라이언트를 다시 시작하세요.`;
}

for (const input of document.querySelectorAll('input[name="auth"]')) {
  input.addEventListener("change", updateCapabilities);
}

document.querySelector("#cancel").addEventListener("click", () => api.closeMcpWizard());
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  resultBox.hidden = true;
  const clients = [...document.querySelectorAll('input[name="client"]:checked')].map((item) => item.value);
  const servers = selectedServerIds();
  if (servers.length === 0) {
    errorBox.textContent = "연결할 서버를 하나 이상 선택하세요.";
    errorBox.hidden = false;
    return;
  }
  if (clients.length === 0) {
    errorBox.textContent = "AI 클라이언트를 하나 이상 선택하세요.";
    errorBox.hidden = false;
    return;
  }
  const policy = authenticationPolicy(selectedServers());
  if (policy.blockedReason) {
    errorBox.textContent = policy.blockedReason;
    errorBox.hidden = false;
    return;
  }
  const authInput = document.querySelector('input[name="auth"]:checked');
  if (!authInput || (authInput.value === "oauth" && !policy.oauthAllowed)) {
    errorBox.textContent = "선택한 서버에 사용할 수 있는 인증 방식을 선택하세요.";
    errorBox.hidden = false;
    return;
  }
  const auth = authInput.value;
  if (auth === "pat" && !tokenInput.value.trim()) {
    errorBox.textContent = "PAT 등록에는 비어 있지 않은 access token이 필요합니다.";
    errorBox.hidden = false;
    return;
  }
  busy(true);
  try {
    const result = await api.registerMcp({
      serverIds: servers,
      clients,
      auth,
      token: auth === "pat" ? tokenInput.value : null,
    });
    const summary = registrationSummary(result);
    renderRegistrationResult(summary);
    resultBox.hidden = false;
    tokenInput.value = "";
  } catch (error) {
    errorBox.textContent = error?.message ?? String(error);
    errorBox.hidden = false;
  } finally {
    busy(false);
    updateCapabilities();
  }
});

api.getMcpState().then(render).catch((error) => {
  const message = error?.message ?? String(error);
  errorBox.textContent = message.replace(
    /^Error invoking remote method '[^']+':\s*(?:Error:\s*)?/,
    "",
  );
  errorBox.hidden = false;
});
