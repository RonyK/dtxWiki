import type { CSSProperties } from "react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

type EmojiOption = {
  emoji: string;
  keywords: string[];
  label: string;
};

const EMOJI_OPTIONS: EmojiOption[] = [
  { emoji: "😀", label: "Grinning face", keywords: ["smile", "happy", "face"] },
  { emoji: "🙂", label: "Slight smile", keywords: ["smile", "calm", "face"] },
  { emoji: "👍", label: "Thumbs up", keywords: ["approve", "yes", "ok"] },
  { emoji: "✅", label: "Check mark", keywords: ["done", "complete", "success"] },
  { emoji: "🚀", label: "Rocket", keywords: ["launch", "ship", "start"] },
  { emoji: "💡", label: "Light bulb", keywords: ["idea", "insight", "note"] },
  { emoji: "🔥", label: "Fire", keywords: ["hot", "important", "energy"] },
  { emoji: "⭐", label: "Star", keywords: ["favorite", "priority", "highlight"] },
  { emoji: "📌", label: "Pushpin", keywords: ["pin", "important", "keep"] },
  { emoji: "📎", label: "Paperclip", keywords: ["attach", "reference", "file"] },
  { emoji: "🧠", label: "Brain", keywords: ["thinking", "knowledge", "context"] },
  { emoji: "⚠️", label: "Warning", keywords: ["risk", "alert", "caution"] },
  { emoji: "❗", label: "Exclamation", keywords: ["important", "attention", "bang"] },
  { emoji: "❓", label: "Question", keywords: ["ask", "unknown", "help"] },
  { emoji: "🎯", label: "Target", keywords: ["goal", "focus", "objective"] },
  { emoji: "📚", label: "Books", keywords: ["library", "docs", "knowledge"] },
  { emoji: "📝", label: "Memo", keywords: ["note", "write", "draft"] },
  { emoji: "🔒", label: "Lock", keywords: ["secure", "private", "permission"] },
  { emoji: "🔗", label: "Link", keywords: ["url", "reference", "chain"] },
  { emoji: "🧪", label: "Test tube", keywords: ["test", "experiment", "lab"] },
];

type EmojiPickerProps = {
  onClose: () => void;
  onSelect: (emoji: string) => void;
  style?: CSSProperties;
};

export function EmojiPicker({ onClose, onSelect, style }: EmojiPickerProps) {
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement | null>(null);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filteredOptions = useMemo(() => {
    if (!normalizedQuery) {
      return EMOJI_OPTIONS;
    }
    return EMOJI_OPTIONS.filter((option) =>
      [option.label, ...option.keywords]
        .join(" ")
        .toLocaleLowerCase()
        .includes(normalizedQuery),
    );
  }, [normalizedQuery]);

  useEffect(() => {
    window.setTimeout(() => searchRef.current?.focus(), 0);
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  }

  return (
    // Escape is handled at the dialog boundary; individual options stay the
    // actual tab stops.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <section
      aria-label="Emoji picker"
      className="emoji-picker"
      onKeyDown={handleKeyDown}
      role="dialog"
      style={style}
      tabIndex={-1}
    >
      <header className="emoji-picker-header">
        <strong>Emoji</strong>
        <button
          aria-label="Close emoji picker"
          className="emoji-picker-close"
          onClick={onClose}
          type="button"
        >
          ×
        </button>
      </header>
      <input
        aria-label="Search emoji"
        className="emoji-picker-search"
        onChange={(event) => setQuery(event.currentTarget.value)}
        placeholder="Search emoji"
        ref={searchRef}
        value={query}
      />
      <div aria-label="Emoji options" className="emoji-picker-grid" role="listbox">
        {filteredOptions.length === 0 ? (
          <p className="emoji-picker-empty">No matching emoji</p>
        ) : (
          filteredOptions.map((option) => (
            <button
              aria-label={`Insert emoji ${option.label}`}
              aria-selected="false"
              className="emoji-picker-option"
              key={option.label}
              onClick={() => onSelect(option.emoji)}
              role="option"
              title={option.label}
              type="button"
            >
              {option.emoji}
            </button>
          ))
        )}
      </div>
    </section>
  );
}
