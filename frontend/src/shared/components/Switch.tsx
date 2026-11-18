import type { ReactNode } from "react";

type SwitchProps = {
  checked: boolean;
  disabled?: boolean;
  id?: string;
  /** Accessible name; the switch itself announces on or off. */
  label: string;
  onChange: (checked: boolean) => void;
  /** Visible state text beside the switch. */
  children?: ReactNode;
};

/** An on/off setting: a native checkbox exposed and drawn as a switch. */
export function Switch({ checked, disabled = false, id, label, onChange, children }: SwitchProps) {
  return (
    <label className="switch-field">
      <input
        aria-label={label}
        checked={checked}
        className="switch-control"
        disabled={disabled}
        id={id}
        onChange={(event) => onChange(event.target.checked)}
        role="switch"
        type="checkbox"
      />
      {children ? <span aria-hidden="true">{children}</span> : null}
    </label>
  );
}
