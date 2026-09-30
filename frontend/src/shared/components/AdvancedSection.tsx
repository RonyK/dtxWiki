import { ChevronDown, ChevronRight, Settings2 } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

type AdvancedSectionProps = {
  children: ReactNode;
  defaultOpen?: boolean;
  label?: string;
  onOpenChange?: (open: boolean) => void;
  open?: boolean;
  /** Overrides the default gear glyph; pass `null` to omit the leading icon. */
  icon?: ReactNode | null;
  /** Muted text after the label, visible in both the open and closed state. */
  subtitle?: ReactNode;
  /** Trailing content (e.g. a count chip), pinned to the toggle's end. */
  trailing?: ReactNode;
};

export function AdvancedSection({
  children,
  defaultOpen = false,
  label = "Advanced",
  onOpenChange,
  open,
  icon,
  subtitle,
  trailing,
}: AdvancedSectionProps) {
  const bodyId = useId();
  const [internalOpen, setInternalOpen] = useState(defaultOpen);
  const expanded = open ?? internalOpen;

  function toggle() {
    const nextOpen = !expanded;
    if (open === undefined) {
      setInternalOpen(nextOpen);
    }
    onOpenChange?.(nextOpen);
  }

  return (
    <section className={`advanced-disclosure ${expanded ? "advanced-disclosure--open" : ""}`}>
      <button
        aria-controls={bodyId}
        aria-expanded={expanded}
        className="advanced-disclosure-toggle"
        onClick={toggle}
        type="button"
      >
        {icon === undefined ? <Settings2 size={14} aria-hidden="true" /> : icon}
        <span>{label}</span>
        {subtitle ? (
          <span className="advanced-disclosure-subtitle">{subtitle}</span>
        ) : null}
        {trailing ? (
          <span className="advanced-disclosure-trailing">{trailing}</span>
        ) : null}
        <span className="advanced-disclosure-chevron" aria-hidden="true">
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </span>
      </button>
      {expanded ? (
        <div className="advanced-disclosure-body" id={bodyId}>
          {children}
        </div>
      ) : null}
    </section>
  );
}
