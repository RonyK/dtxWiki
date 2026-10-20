import { Search, X } from "lucide-react";
import { type KeyboardEvent, useRef } from "react";

import { Spinner } from "./Spinner";

type SearchFieldProps = {
  ariaLabel: string;
  autoComplete?: string;
  clearLabel: string;
  id?: string;
  loading?: boolean;
  maxLength?: number;
  value: string;
  onChange: (value: string) => void;
  onClear?: () => void;
  /**
   * Runs before the built-in Escape-to-clear handling. A filter that lives
   * inside someone else's `<form>` uses this to stop Enter from triggering
   * that form's implicit submit; it must not swallow Escape.
   */
  onKeyDown?: (event: KeyboardEvent<HTMLInputElement>) => void;
  placeholder?: string;
  compact?: boolean;
  submitLabel?: string;
};

export function SearchField({
  ariaLabel,
  autoComplete = "off",
  clearLabel,
  id,
  loading = false,
  maxLength,
  value,
  onChange,
  onClear,
  onKeyDown,
  placeholder,
  compact = false,
  submitLabel,
}: SearchFieldProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);

  function clearSearch() {
    onChange("");
    onClear?.();
    window.requestAnimationFrame(() => {
      if (inputRef.current?.isConnected) inputRef.current.focus();
    });
  }

  return (
    <div
      aria-busy={loading || undefined}
      className={`search-field${compact ? " compact" : ""}`}
    >
      <Search size={15} aria-hidden="true" />
      <input
        aria-label={ariaLabel}
        autoComplete={autoComplete}
        id={id}
        maxLength={maxLength}
        placeholder={placeholder}
        type="search"
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
        onKeyDown={(event) => {
          onKeyDown?.(event);
          if (event.key !== "Escape" || !value) return;
          event.preventDefault();
          clearSearch();
        }}
        ref={inputRef}
      />
      {value ? (
        <button
          aria-label={clearLabel}
          className="search-field-clear"
          type="button"
          onClick={clearSearch}
        >
          <X size={14} aria-hidden="true" />
        </button>
      ) : null}
      {submitLabel ? (
        <button
          className="search-field-submit"
          disabled={loading}
          type="submit"
        >
          {loading ? (
            <Spinner decorative size={14} />
          ) : null}
          <span>{submitLabel}</span>
        </button>
      ) : null}
    </div>
  );
}
