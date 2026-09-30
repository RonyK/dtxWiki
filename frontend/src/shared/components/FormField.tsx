import type { ReactNode } from "react";
import { useId } from "react";

export type FormFieldChildIds = {
  hintId?: string;
  errorId?: string;
};

export type FormFieldChildren =
  | ReactNode
  | ((ids: FormFieldChildIds) => ReactNode);

type FormFieldProps = {
  children: FormFieldChildren;
  className?: string;
  error?: ReactNode;
  errorId?: string;
  hint?: ReactNode;
  hintId?: string;
  htmlFor?: string;
  label: ReactNode;
};

/**
 * Combines one or more id values into a single space-separated string suitable
 * for aria-describedby / aria-labelledby. Returns undefined when nothing is
 * available so the attribute can be omitted.
 */
export function joinIds(
  ...ids: (string | undefined | null | false)[]
): string | undefined {
  const filtered = ids.filter((id): id is string => Boolean(id));
  return filtered.length > 0 ? filtered.join(" ") : undefined;
}

export function FormField({
  children,
  className = "",
  error,
  errorId,
  hint,
  hintId,
  htmlFor,
  label,
}: FormFieldProps) {
  const generatedHintId = useId();
  const generatedErrorId = useId();
  const labelNode = htmlFor ? (
    <label className="form-field-label" htmlFor={htmlFor}>
      {label}
    </label>
  ) : (
    <span className="form-field-label">{label}</span>
  );

  const resolvedHintId = hint ? (hintId ?? generatedHintId) : undefined;
  const resolvedErrorId = error ? (errorId ?? generatedErrorId) : undefined;

  const renderedChildren =
    typeof children === "function"
      ? children({ hintId: resolvedHintId, errorId: resolvedErrorId })
      : children;

  return (
    <div className={`form-field ${className}`.trim()}>
      {labelNode}
      {renderedChildren}
      {hint ? (
        <span className="field-hint" id={resolvedHintId}>
          {hint}
        </span>
      ) : null}
      {error ? (
        <span className="field-error" id={resolvedErrorId} role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
