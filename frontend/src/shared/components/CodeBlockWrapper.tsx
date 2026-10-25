import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

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
  const mountedRef = useRef(true);
  const resetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (resetTimerRef.current !== null) clearTimeout(resetTimerRef.current);
    };
  }, []);

  async function handleCopy() {
    if (!code || !navigator.clipboard?.writeText) {
      return;
    }
    try {
      await navigator.clipboard.writeText(code);
      if (!mountedRef.current) return;
      if (resetTimerRef.current !== null) clearTimeout(resetTimerRef.current);
      setCopied(true);
      resetTimerRef.current = setTimeout(() => {
        resetTimerRef.current = null;
        setCopied(false);
      }, 1600);
    } catch {
      if (mountedRef.current) setCopied(false);
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
