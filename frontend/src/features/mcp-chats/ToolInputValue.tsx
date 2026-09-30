import { Check, Copy } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { canWriteToClipboard, selectText } from "../../shared/lib/clipboard";

// A tool argument long enough that it must not share a `<dl>` grid cell with
// short scalars. Chosen by length rather than by field name so `reason`,
// `risk_notes` and `claim_text` are covered too, not just `body_markdown`.
export const LONG_VALUE_CHARS = 200;

// What we put in the DOM while collapsed. `body_markdown` is capped at ~1 MB
// server-side and the first timeline row renders expanded, so hiding the
// overflow with CSS alone would still mount the whole string.
const COLLAPSED_CHARS = 4000;

export function isLongToolValue(value: unknown): boolean {
  if (typeof value === "string") {
    return value.length >= LONG_VALUE_CHARS;
  }
  return value !== null && typeof value === "object";
}

export function toolValueText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

/**
 * Render one long tool argument as its own full-width block.
 *
 * Shows the source, not rendered markdown: this screen is the record of what a
 * client actually sent, and rendering would drop the very syntax an auditor is
 * checking. The content is also untrusted client input, so leaving it as text
 * avoids handing it a renderer it does not need.
 */
export function ToolInputValue({
  label,
  value,
}: {
  label: string;
  value: unknown;
}) {
  const { t } = useTranslation("chats");
  const [expanded, setExpanded] = useState(false);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "selected">(
    "idle",
  );
  const bodyRef = useRef<HTMLPreElement>(null);
  const text = toolValueText(value);
  const overflows = text.length > COLLAPSED_CHARS;
  const shown = expanded || !overflows ? text : `${text.slice(0, COLLAPSED_CHARS)}…`;
  const lineCount = text.split("\n").length;

  async function copyAll() {
    // The Clipboard API needs a secure context, so a dtxWiki on plain HTTP has
    // no `navigator.clipboard` at all. Falling through to a selection keeps the
    // value reachable; silently doing nothing left the reader clicking a button
    // that never responded, on the only way out of a >4000-char argument.
    const expand = () => {
      setExpanded(true);
      return selectText(bodyRef.current);
    };
    if (!canWriteToClipboard()) {
      setCopyState(expand() ? "selected" : "idle");
      window.setTimeout(() => setCopyState("idle"), 2400);
      return;
    }
    try {
      // Always the full value, never the truncated preview.
      await navigator.clipboard.writeText(text);
      setCopyState("copied");
    } catch {
      setCopyState(expand() ? "selected" : "idle");
    }
    window.setTimeout(() => setCopyState("idle"), 2400);
  }

  return (
    <section className="mcp-tool-longvalue">
      <header className="mcp-tool-longvalue-head">
        <h4>{label}</h4>
        <span className="mcp-tool-longvalue-meta">
          {t("detail.longValue.size", {
            defaultValue: "{{chars}} chars · {{lines}} lines",
            chars: text.length.toLocaleString(),
            lines: lineCount.toLocaleString(),
          })}
        </span>
        <button className="icon-button" type="button" onClick={copyAll}>
          {copyState === "copied" ? (
            <Check size={14} aria-hidden="true" />
          ) : (
            <Copy size={14} aria-hidden="true" />
          )}
          <span>
            {copyState === "copied"
              ? t("detail.longValue.copied", { defaultValue: "Copied" })
              : copyState === "selected"
                ? t("detail.longValue.selected", {
                    defaultValue: "Selected — press Ctrl+C",
                  })
                : t("detail.longValue.copy", { defaultValue: "Copy" })}
          </span>
        </button>
      </header>
      <pre
        ref={bodyRef}
        className="mcp-tool-longvalue-body"
        data-expanded={expanded ? "true" : "false"}
      >
        {shown}
      </pre>
      {overflows ? (
        <button
          className="mcp-strip-toggle"
          type="button"
          onClick={() => setExpanded((open) => !open)}
        >
          {expanded
            ? t("detail.longValue.collapse", { defaultValue: "Show less" })
            : t("detail.longValue.expand", {
                defaultValue: "Show all {{chars}} characters",
                chars: text.length.toLocaleString(),
              })}
        </button>
      ) : null}
    </section>
  );
}
