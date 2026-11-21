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
      "closed",
      "state_inconsistent",
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
      closed: "neutral",
      state_inconsistent: "danger",
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
      closed: { label: "Closed", description: "This draft ended and is preserved as a commit." },
      closed_discarded: {
        label: "Discarded",
        description: "A discarded draft. It stays in History, and you can start again from it.",
      },
      closed_merged: {
        label: "Merged",
        description: "A draft merged into another version, as when it was published by a merge. Its changes are in that version.",
      },
      closed_rejected: {
        label: "Rejected",
        description: "A draft whose review was rejected. You can start again from it.",
      },
      state_inconsistent: {
        label: "State review required",
        description: "Operator repair is required",
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
      closed: { label: "끝남", description: "종료된 초안이며 커밋으로 보존됩니다." },
      closed_discarded: {
        label: "폐기됨",
        description: "폐기한 초안입니다. 히스토리에 남아 있고 이 버전에서 다시 시작할 수 있습니다.",
      },
      closed_merged: {
        label: "병합됨",
        description: "다른 버전에 병합된 초안입니다. 게시할 때 병합된 경우처럼, 변경 내용은 그 버전에 들어 있습니다.",
      },
      closed_rejected: {
        label: "거절됨",
        description: "리뷰에서 거절된 초안입니다. 이 버전에서 다시 시작할 수 있습니다.",
      },
      state_inconsistent: { label: "상태 확인 필요", description: "운영 점검 필요" },
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
