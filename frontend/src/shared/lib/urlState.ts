export function parsePeriodParam<T extends string>(
  raw: string | null,
  validValues: readonly T[],
  defaultValue: T,
): T {
  return validValues.includes(raw as T) ? (raw as T) : defaultValue;
}

/**
 * @deprecated Use applyPeriodParam instead. This helper returns null to signal
 * "omit from URL"; callers must duplicate the URLSearchParams mutation.
 */
export function serializePeriodParam<T extends string>(
  value: T,
  defaultValue: T,
): string | null {
  return value === defaultValue ? null : value;
}

export function applyPeriodParam<T extends string>(
  params: URLSearchParams,
  key: string,
  value: T,
  defaultValue: T,
): URLSearchParams {
  const next = new URLSearchParams(params);
  if (value === defaultValue) {
    next.delete(key);
  } else {
    next.set(key, value);
  }
  return next;
}
