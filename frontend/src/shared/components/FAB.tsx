import { Plus } from "lucide-react";
import type { ReactNode } from "react";

type FABProps = {
  ariaLabel: string;
  busy?: boolean;
  className?: string;
  disabled?: boolean;
  icon?: ReactNode;
  label: string;
  onClick: () => Promise<void> | void;
  title?: string;
};

export function FAB({
  ariaLabel,
  busy = false,
  className,
  disabled = false,
  icon,
  label,
  onClick,
  title,
}: FABProps) {
  const classNames = ["fab", className].filter(Boolean).join(" ");

  return (
    <button
      aria-busy={busy}
      aria-label={ariaLabel}
      className={classNames}
      disabled={disabled}
      onClick={() => {
        void onClick();
      }}
      title={title ?? ariaLabel}
      type="button"
    >
      <span className="fab-icon" aria-hidden="true">
        {icon ?? <Plus size={18} />}
      </span>
      <span className="fab-label">{label}</span>
    </button>
  );
}
