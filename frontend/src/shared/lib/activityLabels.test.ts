import { describe, expect, it } from "vitest";

import type { ActivityItem } from "../api/operations";
import { i18n } from "../i18n";
import {
  activitySubtype,
  isSystemActivityItem,
  presentActivityItem,
} from "./activityLabels";

describe("presentActivityItem", () => {
  it.each([
    ["auth.session.created", "Signed in", "A user session was created."],
    ["auth.session.destroyed", "Signed out", "A user session ended."],
    ["auth.user.signed_up", "Account created", "A new account joined the workspace."],
    ["auth.password.changed", "Password updated", "Account security settings changed."],
    [
      "oauth.authorization.requested",
      "OAuth access requested",
      "An MCP client requested access to this workspace.",
    ],
  ])("maps %s to human-facing copy", (eventType, title, body) => {
    expect(presentActivityItem(activityItem(eventType, eventType))).toEqual({
      title,
      body,
    });
  });

  it("preserves OAuth request event summaries while mapping the body copy", () => {
    expect(
      presentActivityItem(
        activityItem(
          "Codex CLI requested MCP OAuth access",
          "oauth.authorization.requested",
        ),
      ),
    ).toEqual({
      title: "Codex CLI requested MCP OAuth access",
      body: "An MCP client requested access to this workspace.",
    });
  });

  it("preserves explicit backend body copy for known event titles", () => {
    expect(
      presentActivityItem(activityItem("auth.session.created", "Signed in from CLI")),
    ).toEqual({
      title: "Signed in",
      body: "Signed in from CLI",
    });
  });

  it("resolves a domain-coded body to its mapped audit-code label", () => {
    // body "ask.session.create" -> sub-type "ask" -> activity:auditCode.ask,
    // which the activity.json bundle now maps to "Ask activity"; the raw
    // "ask.session.create" code is never surfaced as the title.
    expect(presentActivityItem(activityItem("ask.session.create", "ask.session.create")))
      .toEqual({
        title: "Ask activity",
        body: "",
      });
  });

  it("does not leak a raw audit code in either visible field", () => {
    const presented = presentActivityItem(
      activityItem("Ask turn created", "ask.turn.create"),
    );
    expect(presented.title).not.toContain("ask.turn.create");
    expect(presented.body).not.toContain("ask.turn.create");
  });

  it("does not leak a raw audit code as the title", () => {
    const { title } = presentActivityItem(
      activityItem("library.item.register", "library.item.register"),
    );
    expect(title).not.toBe("library.item.register");
  });

  it.each([
    [
      "sensitive AgentPage draft requires owner activation review",
      "sensitive AgentPage draft requires owner activation review",
    ],
    ["context draft requires review", "context draft requires review"],
    [
      "context update draft requires review",
      "context update draft requires review",
    ],
  ])("preserves the unkeyed pending review summary %s", (summary, expected) => {
    // The server repeats decision_summary in body and target_title. Neither is
    // a presentation key, so render the stored value unchanged.
    expect(presentActivityItem(reviewItem(summary))).toEqual({
      title: expected,
      body: "Change set",
    });
  });

  it("translates a keyed event for a Korean reader and preserves an unkeyed fallback", async () => {
    const previousLanguage = i18n.language;
    await i18n.changeLanguage("ko");
    try {
      expect(
        presentActivityItem({
          ...activityItem("tree.node.rename", "tree.node.rename"),
          event_key: "tree.node.rename",
          event_args: { previous_title: "Old", title: "New" },
        }).title,
      ).toBe("이름 변경: Old → New");
      expect(
        presentActivityItem({
          ...activityItem("Legacy summary remains as written", "legacy.event"),
          target_title: "Current page title",
        }).title,
      ).toBe("Legacy summary remains as written");
    } finally {
      await i18n.changeLanguage(previousLanguage);
    }
  });

  it("renders a keyed join-request title, not the humanized raw audit code (UI-08)", () => {
    // Before the backend fix, workspace.join_request.* events always carried
    // event_key: null with title/body set to the raw action string, so every
    // row (owner or not) fell through to auditCodeLabel -> humanizeEnum,
    // which does not treat "." as a word boundary and produced the same
    // garbled "Workspace.join request.requested" for every request. This
    // pins the now-fixed (owner-visible, event_key present) shape.
    expect(
      presentActivityItem({
        ...activityItem(
          "Workspace join request submitted",
          "workspace.join_request.requested",
        ),
        event_key: "workspace.join_request.requested",
        event_args: { attempt_no: "1" },
      }).title,
    ).toBe("Requested to join the workspace");
  });

  it("keeps author-written review notes verbatim", () => {
    const summary = "Legal asked for the pricing table to be re-checked";
    expect(presentActivityItem(reviewItem(summary)).title).toBe(summary);
  });

  it("renders job diagnostics as sentences instead of key=value tokens", () => {
    const presented = presentActivityItem({
      ...jobItem("Extraction running"),
      body: "trigger=library_registration; progress=0%",
      target_title: "Extraction",
    });
    expect(presented).toEqual({
      title: "Extraction",
      body: "Triggered by a new source · Starting",
    });
  });

  it("translates the internal job_type title into reader-facing copy (M-11)", () => {
    // The backend titles a job row by title-casing job_type
    // ("feedback_resolution" -> "Feedback Resolution"), which reads as an
    // internal class name rather than something a reader wrote or asked for.
    const presented = presentActivityItem({
      ...jobItem("Feedback Resolution succeeded"),
      body: "trigger=feedback_submit; progress=100%",
      target_title: "Feedback Resolution",
    });
    expect(presented.title).toBe("Feedback triage");

    const lint = presentActivityItem({
      ...jobItem("Lint Tick succeeded"),
      body: "trigger=scheduled; progress=100%",
      target_title: "Lint Tick",
    });
    expect(lint.title).toBe("Lint sweep");
  });

  it("keeps 0% literal (not \"Starting\") once it sits beside a failure code", () => {
    // M-12 (260908 UI audit): "Starting" next to "The job failed." would
    // contradict itself -- 0% + error_code means the job failed AT 0%, not
    // that it merely hasn't started yet. Only a bare 0% (no error_code) reads
    // as pending/stalled and gets replaced.
    const presented = presentActivityItem({
      ...jobItem("Extraction failed"),
      body: "trigger=manual; progress=0%; error_code=job_failed",
      target_title: "Extraction",
    });
    expect(presented.body).toBe(
      "Triggered by a manual run · 0% complete · The job failed.",
    );
  });

  it("names the tick scheduler's trigger", () => {
    // worker/tick.py seeds lint_tick/trail_tick/librarian_tick with
    // trigger_type="scheduled" — the untranslated fallback would leak that
    // English token into the ko feed.
    const presented = presentActivityItem({
      ...jobItem("Librarian Tick succeeded"),
      body: "trigger=scheduled; progress=100%",
      target_title: "Librarian Tick",
    });
    expect(presented.body).toBe("Triggered by a schedule · 100% complete");
  });

  it("renders a job failure code as a sentence instead of a raw diagnostic key", async () => {
    // The backend ships a closed vocabulary
    // (job_read_service.PUBLIC_JOB_FAILURE_CODES) rather than a rendered
    // sentence, precisely so this layer owns the copy: a sentence on the wire
    // would pass through formatActivityDetail untouched and reach a Korean
    // reader in English.
    const presented = presentActivityItem({
      ...jobItem("Extraction failed"),
      body: "trigger=manual; progress=0%; error_code=job_failed",
      target_title: "Extraction",
    });
    expect(presented.body).toBe(
      "Triggered by a manual run · 0% complete · The job failed.",
    );

    const retrying = presentActivityItem({
      ...jobItem("Extraction running"),
      body: "trigger=manual; progress=0%; error_code=job_retry_scheduled",
      target_title: "Extraction",
    });
    expect(retrying.body).toBe(
      "Triggered by a manual run · 0% complete · An attempt failed; a retry is scheduled.",
    );

    // An unrecognised/newer code must degrade to a generic sentence, never to
    // humanizeEnum's raw English key — the point of the code is that no
    // diagnostic text ships.
    const unknown = presentActivityItem({
      ...jobItem("Extraction failed"),
      body: "trigger=manual; progress=0%; error_code=job_future_code",
      target_title: "Extraction",
    });
    expect(unknown.body).toBe(
      "Triggered by a manual run · 0% complete · The job reported a failure.",
    );

    const previousLanguage = i18n.language;
    await i18n.changeLanguage("ko");
    try {
      expect(presentActivityItem({
        ...jobItem("Extraction failed"),
        body: "trigger=manual; progress=0%; error_code=job_failed",
        target_title: "Extraction",
      }).body).toBe(
        "시작 계기: 수동 실행 · 진행률 0% · 작업이 실패했습니다.",
      );
    } finally {
      await i18n.changeLanguage(previousLanguage);
    }
  });

  it("drops the status token the MCP row title already carries", () => {
    const presented = presentActivityItem({
      ...activityItem(
        "MCP update draft failed",
        "Claude Code; status=failed; Tool call failed.",
      ),
      actor: {
        actor_kind: "mcp_client",
        actor_id: "client-1",
        display_name: "Claude Code",
        is_current_user: false,
      },
      target: { target_kind: "mcp_tool_call", target_id: "call-1" },
      target_title: "Claude Code",
    });
    expect(presented.body).toBe("Claude Code · Tool call failed.");
  });

  it("replaces raw validation schema details with localized user-facing copy", async () => {
    const item = activityItem(
      "Workspace action failed",
      "validation_failed; workspace_id: Field required; workspace_slug: Extra inputs are not permitted; request_id=secret",
    );
    expect(presentActivityItem(item).body).toBe(
      "The action could not be completed because its input was invalid.",
    );

    const previousLanguage = i18n.language;
    await i18n.changeLanguage("ko");
    try {
      expect(presentActivityItem(item).body).toBe(
        "입력값이 올바르지 않아 작업을 완료하지 못했습니다.",
      );
    } finally {
      await i18n.changeLanguage(previousLanguage);
    }
  });

  it("identifies auth events as system activity", () => {
    expect(isSystemActivityItem(activityItem("auth.session.created", ""))).toBe(true);
    expect(isSystemActivityItem(activityItem("Signed in", "auth.session.created")))
      .toBe(true);
    expect(
      isSystemActivityItem(
        activityItem("Codex CLI requested MCP OAuth access", "oauth.authorization.requested"),
      ),
    ).toBe(true);
    expect(isSystemActivityItem(activityItem("ask.turn.created", "Ask turn created")))
      .toBe(false);
  });
});

describe("isSystemActivityItem (v3 platform/operations)", () => {
  it("classifies lint audit rows as system", () => {
    expect(isSystemActivityItem(activityItem("Lint run", "lint.run.create"))).toBe(
      true,
    );
  });

  it("classifies background jobs (target_kind=job) as system", () => {
    expect(isSystemActivityItem(jobItem("Library Extraction failed"))).toBe(true);
  });

  it("keeps workspace content out of system", () => {
    expect(
      isSystemActivityItem(activityItem("Page updated", "context_page.update")),
    ).toBe(false);
  });
});

describe("activitySubtype", () => {
  it.each([
    ["library.item.register", "library"],
    ["context_page.draft.create", "context"],
    ["page.update", "context"],
    ["context_ingress.proposal.create", "context"],
    ["tree.node.move", "context"],
    ["feedback.submit", "context"],
    ["agent_page.activated", "agent"],
    ["bundle.build", "agent"],
    ["ask.session.create", "ask"],
    ["review.decision", "review"],
    ["mcp_chat_session.archived", "chat"],
  ])("maps workspace action %s to %s", (action, subtype) => {
    expect(activitySubtype(activityItem("x", action))).toBe(subtype);
  });

  it.each([
    ["auth.session.created", "auth"],
    ["oauth.consent.granted", "oauth"],
    ["lint.run.create", "lint"],
    ["settings.policy.review.updated", "settings"],
  ])("maps system action %s to %s", (action, subtype) => {
    expect(activitySubtype(activityItem("x", action))).toBe(subtype);
  });

  it("maps background jobs to the jobs sub-type", () => {
    expect(activitySubtype(jobItem("Library Extraction failed"))).toBe("jobs");
  });

  it.each([
    ["MCP read page completed", "read"],
    ["MCP search succeeded", "search"],
    ["MCP write draft succeeded", "write"],
    ["MCP build agent input bundle failed", "write"],
    ["MCP read rejected_by_policy", "read"],
  ])("maps MCP title %s to %s", (title, subtype) => {
    expect(activitySubtype(activityItem(title, title))).toBe(subtype);
  });
});

function activityItem(title: string, body: string): ActivityItem {
  return {
    id: "activity-1",
    lane: "history",
    event_key: null,
    event_args: {},
    title,
    body,
    actor: null,
    target: null,
    created_at: "2026-05-19T00:00:00Z",
    review_item_id: null,
    affordances: [],
  };
}

// A pending-review row as activity_service._pending_reviews builds it: the
// ReviewItem summary lands in body and target_title, while the title is the
// review type.
function reviewItem(summary: string): ActivityItem {
  return {
    ...activityItem("Review required: context_update", summary),
    lane: "pending",
    target: { target_kind: "change_set", target_id: "change-1" },
    target_title: summary,
    review_item_id: "review-1",
  };
}

function jobItem(title: string): ActivityItem {
  return {
    ...activityItem(title, "trigger=manual; progress=0%"),
    target: { target_kind: "job", target_id: "job-1" },
  };
}
