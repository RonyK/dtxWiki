import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";

import {
  createPageLifecycleRecorder,
  runWithCompletedResponse,
} from "./page-diagnostics.ts";

const origin = "https://app.e2e.invalid";
const endpoint = "/api/v1/workspaces/workspace-id/members/member-id";
const request = (url = `${origin}${endpoint}`, method = "DELETE") => ({
  url: () => url,
  method: () => method,
  failure: () => ({ errorText: "net::ERR_CONNECTION_RESET" }),
});
const response = (sent, status = 200) => ({
  request: () => sent,
  status: () => status,
});
const matches = (sent) => new URL(sent.url()).pathname === endpoint && sent.method() === "DELETE";
const assertDetached = (page) => assert.deepEqual(page.eventNames(), []);

test("waits for the exact response body, even if another matching request finishes", async () => {
  const page = new EventEmitter();
  const sent = request();
  const headers = response(sent);
  let complete = false;
  let started;
  const actionStarted = new Promise((resolve) => { started = resolve; });
  const pending = runWithCompletedResponse(page, matches, async () => {
    page.emit("response", headers);
    page.emit("requestfinished", request());
    started();
  }).then((value) => { complete = true; return value; });
  await actionStarted;
  assert.equal(complete, false, "headers and a different request must not complete the wait");
  page.emit("requestfinished", sent);
  assert.equal(await pending, headers);
  assertDetached(page);
});

for (const receivedHeaders of [false, true]) {
  test(`bounds ${receivedHeaders ? "an unfinished body" : "missing headers"} and releases listeners`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const page = new EventEmitter();
    let started;
    const actionStarted = new Promise((resolve) => { started = resolve; });
    const pending = runWithCompletedResponse(page, matches, async () => {
      if (receivedHeaders) page.emit("response", response(request()));
      started();
    });
    const rejected = assert.rejects(pending, receivedHeaders ? /body unfinished/ : /no matching response headers/);
    await actionStarted;
    t.mock.timers.tick(15_000);
    await rejected;
    assertDetached(page);
  });
}

test("reports a matching transport failure before headers without retaining raw errors", async () => {
  const page = new EventEmitter();
  const sent = request(`${origin}${endpoint}?token=never-retain-this`);
  sent.failure = () => ({ errorText: "failed https://secret.invalid/?token=never-retain-this" });
  await assert.rejects(runWithCompletedResponse(page, matches, async () => {
    page.emit("requestfailed", sent);
  }), { message: "Matched request failed before its response body completed" });
  assertDetached(page);
});

test("cleans up after a synchronous action failure", async () => {
  const page = new EventEmitter();
  const failure = new Error("action failed");
  await assert.rejects(runWithCompletedResponse(page, matches, () => { throw failure; }), failure);
  assertDetached(page);
});

for (const event of ["close", "crash"]) {
  test(`reports page ${event} while waiting for a response body`, async () => {
    const page = new EventEmitter();
    await assert.rejects(runWithCompletedResponse(page, matches, async () => {
      page.emit("response", response(request()));
      page.emit(event);
    }), event === "close" ? /Page closed/ : /Page crashed/);
    assertDetached(page);
  });
}

test("a successful response clears its deadline", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const page = new EventEmitter();
  const sent = request();
  await runWithCompletedResponse(page, matches, async () => {
    page.emit("response", response(sent));
    page.emit("requestfinished", sent);
  });
  t.mock.timers.tick(15_000);
  assertDetached(page);
});

test("lifecycle metadata joins headers and body completion without retaining credentials", () => {
  const page = new EventEmitter();
  const recorder = createPageLifecycleRecorder(page);
  const sent = request("https://user:password@app.e2e.invalid/mcp/session/dtxw_private?token=query-secret#fragment-secret");
  sent.failure = () => ({ errorText: "failure contains query-secret" });
  page.emit("request", sent);
  page.emit("response", response(sent, 503));
  page.emit("requestfinished", sent);
  page.emit("requestfailed", sent);
  page.emit("domcontentloaded");
  page.emit("load");
  const snapshot = recorder.snapshot();
  assert.deepEqual(snapshot.events.map(({ event }) => event), [
    "request", "response", "requestfinished", "requestfailed", "domcontentloaded", "load",
  ]);
  assert.deepEqual(snapshot.events.slice(0, 4).map(({ request_id }) => request_id), [1, 1, 1, 1]);
  assert.equal(snapshot.events[1].status, 503);
  assert.equal(snapshot.events[0].pathname, "/mcp/session/REDACTED");
  assert.equal(snapshot.events[3].errorText, "REDACTED");
  assert.doesNotMatch(JSON.stringify(snapshot), /user:|password|dtxw_private|query-secret|fragment-secret/);
  recorder.stop();
  assertDetached(page);
  page.emit("load");
  assert.equal(recorder.snapshot().events.length, 6);
});

test("lifecycle metadata keeps recent events within a fixed bound", () => {
  const page = new EventEmitter();
  const recorder = createPageLifecycleRecorder(page);
  for (let index = 0; index < 200; index += 1) page.emit("request", request());
  const snapshot = recorder.snapshot();
  assert.equal(snapshot.events.length, 160);
  assert.equal(snapshot.dropped_events, 40);
  assert.equal(snapshot.events[0].request_id, 41);
  recorder.stop();
  assertDetached(page);
});
