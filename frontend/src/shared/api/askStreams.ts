import { apiEventStream, apiRequest } from "./client";
import type { AskTurnDetail, EnvelopeWarning } from "./operations";
import type { components } from "./types.gen";

export type AskModelSelection = components["schemas"]["AskModelSelection"];
export type AskModelOption = components["schemas"]["AskModelChoice"];
type AskSubmission = components["schemas"]["AskStreamTurnCreate"];

type TurnEvent =
  | { kind: "state"; turn: AskTurnDetail }
  | { kind: "delta"; text: string; reset?: boolean };

export async function listAskModels(workspaceId: string, signal: AbortSignal) {
  const response = await apiRequest<{ data: { items: AskModelOption[] } }>(
    `/api/v1/workspaces/${workspaceId}/ask/models`,
    { signal },
  );
  return response.data.items;
}

export async function submitAskQuestion(
  workspaceId: string,
  sessionId: string,
  request: AskSubmission,
) {
  const response = await apiRequest<{ data: AskTurnDetail; envelope?: { warnings?: EnvelopeWarning[] } }>(
    `/api/v1/workspaces/${workspaceId}/ask/sessions/${sessionId}/turns/stream`,
    { method: "POST", body: JSON.stringify(request) },
  );
  return { turn: response.data, warnings: response.envelope?.warnings ?? [] };
}

export async function steerAskTurn(
  workspaceId: string,
  turnId: string,
  userMessage: string,
  requestId: string,
) {
  const response = await apiRequest<{ data: AskTurnDetail }>(
    `/api/v1/workspaces/${workspaceId}/ask/turns/${turnId}/steer`,
    { method: "POST", body: JSON.stringify({ user_message: userMessage, request_id: requestId }) },
  );
  return response.data;
}

export async function stopAskTurn(workspaceId: string, turnId: string) {
  const response = await apiRequest<{ data: AskTurnDetail }>(
    `/api/v1/workspaces/${workspaceId}/ask/turns/${turnId}/stop`,
    { method: "POST" },
  );
  return response.data;
}

export async function searchAskQuestion(
  workspaceId: string,
  turnId: string,
  signal: AbortSignal,
) {
  const response = await apiRequest<{ data: AskTurnDetail }>(
    `/api/v1/workspaces/${workspaceId}/ask/turns/${turnId}/search`,
    { signal },
  );
  return response.data;
}

export async function followAskTurn(
  workspaceId: string,
  turnId: string,
  signal: AbortSignal,
  onEvent: (event: TurnEvent) => void,
) {
  const response = await apiEventStream(
    `/api/v1/workspaces/${workspaceId}/ask/turns/${turnId}/stream`,
    signal,
  );
  const contentType = response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase();
  if (!response.body || contentType !== "text/event-stream") {
    throw new Error("Expected an Ask event stream");
  }

  const reader = response.body.getReader();
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener("abort", cancel, { once: true });
  const decoder = new TextDecoder();
  let remainder = "";
  let eventName = "";
  let data: string[] = [];

  function handleLine(line: string) {
    if (line === "") {
      const payload = data.join("\n");
      if (payload && eventName === "state") {
        onEvent({ kind: "state", turn: JSON.parse(payload) as AskTurnDetail });
      } else if (payload && eventName === "delta") {
        const delta = JSON.parse(payload) as { text: string; reset?: boolean };
        onEvent({ kind: "delta", text: delta.text, reset: delta.reset });
      }
      eventName = "";
      data = [];
      return;
    }
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    const value = separator === -1 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "event") eventName = value;
    if (field === "data") data.push(value);
  }

  try {
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done) return;
      remainder += decoder.decode(chunk.value, { stream: true });
      const lines = remainder.split(/\r\n|\n|\r(?!$)/);
      remainder = lines.pop() ?? "";
      for (const line of lines) {
        if (signal.aborted) return;
        handleLine(line);
      }
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    cancel();
    reader.releaseLock();
  }
}
