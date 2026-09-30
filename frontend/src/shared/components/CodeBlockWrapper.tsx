import { Check, Copy } from "lucide-react";
import { useState, type ReactNode } from "react";

type CodeBlockWrapperProps = {
  children: ReactNode;
  code: string;
  language: string;
  sourceAnchorAttributes?: {
    "data-source-line"?: string | number;
    "data-source-end-line"?: string | number;
    "data-scroll-sync-anchor"?: string;
  };
};

export function CodeBlockWrapper({
  children,
  code,
  language,
  sourceAnchorAttributes,
}: CodeBlockWrapperProps) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    if (!code || !navigator.clipboard?.writeText) {
      return;
    }
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  return (
    <figure {...sourceAnchorAttributes} className="markdown-code-block">
      <figcaption className="markdown-code-toolbar">
        <span className="markdown-code-language">{language}</span>
        <button
          aria-label={copied ? "Code copied" : "Copy code"}
          className="markdown-code-copy"
          disabled={!code}
          onClick={() => void handleCopy()}
          title={copied ? "Code copied" : "Copy code"}
          type="button"
        >
          {copied ? (
            <Check size={14} aria-hidden="true" />
          ) : (
            <Copy size={14} aria-hidden="true" />
          )}
        </button>
      </figcaption>
      {children}
    </figure>
  );
}
