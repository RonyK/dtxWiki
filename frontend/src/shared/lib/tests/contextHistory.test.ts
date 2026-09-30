import { describe, expect, it } from "vitest";

import enContext from "../../i18n/locales/en/context.json";
import enPages from "../../i18n/locales/en/pages.json";
import koContext from "../../i18n/locales/ko/context.json";
import koPages from "../../i18n/locales/ko/pages.json";
import {
  CONTEXT_HISTORY_PRESENTATION,
  CONTEXT_HISTORY_STATUSES,
  contextHistoryPillTone,
} from "../contextHistory";

describe("Context history presentation", () => {
  it("maps every lifecycle state to one exact non-heuristic tone", () => {
    expect(CONTEXT_HISTORY_STATUSES).toEqual([
      "state_inconsistent",
      "current_editing",
      "current_draft",
      "published_snapshot",
      "alternative_draft",
      "discarded_draft",
      "superseded_snapshot",
      "superseded_draft",
    ]);
    expect(
      Object.fromEntries(
        CONTEXT_HISTORY_STATUSES.map((status) => [
          status,
          CONTEXT_HISTORY_PRESENTATION[status].tone,
        ]),
      ),
    ).toEqual({
      state_inconsistent: "danger",
      current_editing: "info",
      current_draft: "warn",
      published_snapshot: "ok",
      alternative_draft: "info",
      discarded_draft: "muted",
      superseded_snapshot: "neutral",
      superseded_draft: "neutral",
    });
    expect(contextHistoryPillTone("superseded_snapshot")).toBe("muted");
  });

  it("keeps the authoritative English and Korean labels and descriptions in both readers", () => {
    const expectedEnglish = {
      state_inconsistent: {
        label: "State review required",
        description: "Operator repair is required",
      },
      current_editing: {
        label: "Currently editing",
        description:
          "A private, unsubmitted edit in progress. Visible only to its author until submitted for review.",
      },
      current_draft: {
        label: "Current draft",
        description:
          "The current edit is not published yet. Readers still see the published version.",
      },
      published_snapshot: {
        label: "Current published snapshot",
        description: "The version readers see.",
      },
      alternative_draft: {
        label: "Alternative draft",
        description: "Preserved outside the current editing path.",
      },
      discarded_draft: {
        label: "Discarded draft",
        description: "Removed from the active alternatives.",
      },
      superseded_snapshot: {
        label: "Superseded snapshot",
        description: "Replaced by a newer published version.",
      },
      superseded_draft: {
        label: "Superseded draft",
        description: "Replaced by a newer version.",
      },
    };
    const expectedKorean = {
      state_inconsistent: { label: "상태 확인 필요", description: "운영 점검 필요" },
      current_editing: {
        label: "편집 중",
        description: "아직 제출되지 않은 비공개 편집본. 제출 전까지 작성자 본인에게만 보임",
      },
      current_draft: {
        label: "현재 초안",
        description: "아직 게시되지 않은 현재 편집본. 독자는 현재 게시본을 봄",
      },
      published_snapshot: {
        label: "현재 게시 스냅샷",
        description: "독자가 보는 버전",
      },
      alternative_draft: {
        label: "대안 초안",
        description: "현재 편집 경로와 분기되어 보존됨",
      },
      discarded_draft: { label: "폐기된 초안", description: "대안에서 제외됨" },
      superseded_snapshot: {
        label: "대체된 스냅샷",
        description: "더 최신 게시본으로 대체됨",
      },
      superseded_draft: {
        label: "대체된 초안",
        description: "더 최신 버전으로 대체됨",
      },
    };

    expect(enContext.history.states).toEqual(expectedEnglish);
    expect(enPages.history.states).toEqual(expectedEnglish);
    expect(koContext.history.states).toEqual(expectedKorean);
    expect(koPages.history.states).toEqual(expectedKorean);
  });
});
