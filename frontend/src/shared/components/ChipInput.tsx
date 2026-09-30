import { X } from "lucide-react";
import { useId, useRef, useState, type KeyboardEvent } from "react";

export type ChipInputProps = {
  ariaDescribedBy?: string;
  ariaInvalid?: boolean;
  ariaLabel: string;
  disabled?: boolean;
  editableItems?: boolean;
  getEditAriaLabel?: (item: string) => string;
  getRemoveAriaLabel?: (item: string) => string;
  id?: string;
  inputDisabled?: boolean;
  maxItemLength?: number;
  onChange: (value: string[]) => void | boolean;
  onDuplicate?: (item: string) => void;
  onEditingChange?: (editing: boolean) => void;
  onItemTooLong?: (item: string) => void;
  onQueryChange?: (query: string) => void;
  placeholder?: string;
  persistentPlaceholder?: boolean;
  stripLeadingHash?: boolean;
  suggestions?: string[];
  truncateItems?: boolean;
  value: string[];
};

export function ChipInput({
  ariaDescribedBy,
  ariaInvalid = false,
  ariaLabel,
  disabled = false,
  editableItems = false,
  getEditAriaLabel = (item) => `Edit ${item}`,
  getRemoveAriaLabel = (item) => `Remove ${item}`,
  id,
  inputDisabled = false,
  maxItemLength = 48,
  onChange,
  onDuplicate,
  onEditingChange,
  onItemTooLong,
  onQueryChange,
  placeholder,
  persistentPlaceholder = false,
  stripLeadingHash = true,
  suggestions = [],
  truncateItems = true,
  value,
}: ChipInputProps) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [editingItem, setEditingItem] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const visibleSuggestions = suggestions.filter(
    (suggestion) => !value.some((tag) => tag.toLocaleLowerCase() === suggestion.toLocaleLowerCase()),
  );

  function updateQuery(nextQuery: string) {
    setQuery(nextQuery);
    onQueryChange?.(nextQuery);
  }

  function parseTags(rawValue: string) {
    const nextTags = rawValue
      .split(",")
      .map((tag) => {
        const trimmed = tag.trim().replace(/\s+/g, " ");
        const value = stripLeadingHash ? trimmed.replace(/^#+/, "") : trimmed;
        return value;
      })
      .filter(Boolean);
    if (!truncateItems) {
      const tooLong = nextTags.find((tag) => tag.length > maxItemLength);
      if (tooLong) {
        onItemTooLong?.(tooLong);
        return null;
      }
      return nextTags;
    }
    return nextTags.map((tag) => tag.slice(0, maxItemLength).trim());
  }

  function addTags(rawValue: string) {
    const nextTags = parseTags(rawValue);
    if (nextTags === null) return;
    if (nextTags.length === 0) {
      return;
    }
    const seen = new Set(value.map((tag) => tag.toLocaleLowerCase()));
    const merged = [...value];
    let addedAny = false;
    for (const tag of nextTags) {
      const key = tag.toLocaleLowerCase();
      if (seen.has(key)) {
        onDuplicate?.(tag);
        continue;
      }
      seen.add(key);
      merged.push(tag);
      addedAny = true;
    }
    if (!addedAny) {
      inputRef.current?.select();
      return;
    }
    const accepted = onChange(merged);
    if (accepted !== false) {
      updateQuery("");
    } else {
      inputRef.current?.select();
    }
  }

  function removeTag(tagToRemove: string) {
    if (editingItem === tagToRemove) {
      stopEditing();
    }
    onChange(value.filter((tag) => tag !== tagToRemove));
  }

  function startEditing(tag: string) {
    if (disabled || !editableItems) return;
    setEditingItem(tag);
    setEditingValue(tag);
    onEditingChange?.(true);
  }

  function stopEditing() {
    setEditingItem(null);
    setEditingValue("");
    onEditingChange?.(false);
  }

  function commitEdit(previous: string, rawValue: string) {
    const parsed = parseTags(rawValue);
    if (parsed === null) return;
    const [next] = parsed;
    if (!next) {
      stopEditing();
      return;
    }
    const normalized = next.toLocaleLowerCase();
    const duplicate = value.some(
      (item) => item !== previous && item.toLocaleLowerCase() === normalized,
    );
    if (!duplicate && next !== previous) {
      onChange(value.map((item) => item === previous ? next : item));
    }
    stopEditing();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      addTags(query);
      return;
    }
    if (event.key === "Backspace" && !query && value.length > 0) {
      onChange(value.slice(0, -1));
    }
  }

  function handleInputBlur() {
    if (query.trim()) addTags(query);
  }

  return (
    <div className="chip-input">
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions -- redirects focus to the already-focusable, already-labeled input below; adds no new interactive semantics */}
      <div
        className="chip-input-control"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) {
            event.preventDefault();
            inputRef.current?.focus();
          }
        }}
      >
        {value.map((tag) => (
          <span className="chip-input-chip" key={tag}>
            {editableItems ? (
              editingItem === tag ? (
                <input
                  aria-label={getEditAriaLabel(tag)}
                  autoFocus
                  className="chip-input-chip-edit-input"
                  disabled={disabled}
                  maxLength={maxItemLength}
                  onBlur={(event) => commitEdit(tag, event.currentTarget.value)}
                  onChange={(event) => setEditingValue(event.currentTarget.value)}
                  onKeyDown={(event) => {
                    if (event.nativeEvent.isComposing) return;
                    if (event.key === "Enter") {
                      event.preventDefault();
                      event.stopPropagation();
                      commitEdit(tag, event.currentTarget.value);
                    } else if (event.key === "Escape") {
                      event.preventDefault();
                      event.stopPropagation();
                      stopEditing();
                    }
                  }}
                  value={editingValue}
                />
              ) : (
                <button
                  aria-label={getEditAriaLabel(tag)}
                  className="chip-input-chip-value"
                  disabled={disabled}
                  onClick={() => startEditing(tag)}
                  type="button"
                >
                  {tag}
                </button>
              )
            ) : (
              <span className="chip-input-chip-label">{tag}</span>
            )}
            <button
              aria-label={getRemoveAriaLabel(tag)}
              className="chip-input-chip-remove"
              disabled={disabled}
              onClick={() => removeTag(tag)}
              type="button"
            >
              <X size={13} aria-hidden="true" />
            </button>
          </span>
        ))}
        <input
          aria-describedby={ariaDescribedBy}
          aria-invalid={ariaInvalid || undefined}
          aria-label={ariaLabel}
          disabled={disabled || inputDisabled}
          id={inputId}
          ref={inputRef}
          onBlur={handleInputBlur}
          onChange={(event) => updateQuery(event.currentTarget.value)}
          onKeyDown={handleKeyDown}
          placeholder={persistentPlaceholder || value.length === 0 ? placeholder : undefined}
          value={query}
        />
      </div>
      {visibleSuggestions.length > 0 && query.trim() ? (
        <div className="chip-input-suggestions" role="listbox">
          {visibleSuggestions.map((suggestion) => (
            <button
              aria-selected={false}
              disabled={disabled}
              key={suggestion}
              onClick={() => addTags(suggestion)}
              role="option"
              type="button"
            >
              {suggestion}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
