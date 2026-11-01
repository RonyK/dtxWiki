import { useEffect, useState } from "react";

import { previewLinkTargets } from "../../shared/api/linkTargets";
import { newIdempotencyKey } from "../../shared/api/pages";
import type { ContextLinkTarget } from "../../shared/lib/contextLinks";
import { codePointLength } from "../../shared/lib/inputPolicy";

type PreviewInput = {
  actorId: string | null;
  body: string;
  enabled: boolean;
  sourcePageId?: string | null;
  sourceVersionId?: string | null;
  workspaceId: string | null;
};

type PreviewResult = {
  actorId: string | null;
  body: string;
  sourcePageId: string | null;
  sourceVersionId: string | null;
  workspaceId: string;
  targets: ContextLinkTarget[];
};

export function useLinkTargetPreview({
  actorId,
  body,
  enabled,
  sourcePageId = null,
  sourceVersionId = null,
  workspaceId,
}: PreviewInput): ContextLinkTarget[] {
  const [result, setResult] = useState<PreviewResult | null>(null);

  useEffect(() => {
    setResult(null);
    if (!enabled || !workspaceId || codePointLength(body) > 1_000_000) return;
    const controller = new AbortController();
    const requestId = newIdempotencyKey();
    // Keep typing responsive in split mode without asking the server on every
    // keystroke. Cleanup invalidates even transports that ignore abort signals.
    const timeout = window.setTimeout(() => {
      void previewLinkTargets(workspaceId, {
        body_md: body,
        source_page_id: sourcePageId,
        source_version_id: sourceVersionId,
        request_id: requestId,
      }, controller.signal).then((preview) => {
        if (
          controller.signal.aborted ||
          preview.workspace_id !== workspaceId ||
          preview.source_page_id !== sourcePageId ||
          preview.source_version_id !== sourceVersionId ||
          preview.request_id !== requestId
        ) return;
        setResult({
          actorId, body, sourcePageId, sourceVersionId, workspaceId,
          targets: (preview.link_targets ?? []).map(target => ({
            ...target,
            href: target.href ?? null,
            target_page_id: target.target_page_id ?? null,
          })),
        });
      }).catch(() => {
        // The reader can still display the authored buffer. Unconfirmed index
        // refs remain unavailable, and no previous buffer's targets are reused.
      });
    }, 200);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [actorId, body, enabled, sourcePageId, sourceVersionId, workspaceId]);

  // Compare the exact buffer, including Markdown hard-break whitespace, during
  // render so an older result disappears before the next effect executes.
  return enabled && result?.body === body &&
    result.actorId === actorId &&
    result.workspaceId === workspaceId &&
    result.sourcePageId === sourcePageId &&
    result.sourceVersionId === sourceVersionId
    ? result.targets
    : [];
}
