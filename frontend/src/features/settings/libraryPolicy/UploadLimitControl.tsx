import { useState } from "react";
import { useTranslation } from "react-i18next";

export type ByteSizeUnitValue = "bytes" | "kb" | "mb" | "gib";

export type ByteSizeDraft = {
  bytes: number;
  unit: ByteSizeUnitValue;
};

const BYTE_SIZE_UNITS: Array<{
  value: ByteSizeUnitValue;
  label: string;
  multiplier: number;
}> = [
  { value: "bytes", label: "Bytes", multiplier: 1 },
  { value: "kb", label: "KB", multiplier: 1_000 },
  { value: "mb", label: "MB", multiplier: 1_000_000 },
  { value: "gib", label: "GiB", multiplier: 1_073_741_824 },
];

/**
 * One decimal byte-limit control for Workspace defaults, format families, and
 * exact format preferences. Keeping the amount/unit behavior here prevents
 * those three policy layers from drifting visually or numerically.
 */
export function UploadLimitControl({
  busy,
  fieldId,
  label,
  maxBytes,
  minBytes = 1_000,
  onChange,
  value,
}: {
  busy: boolean;
  fieldId: string;
  label: string;
  maxBytes?: number;
  minBytes?: number;
  onChange: (value: ByteSizeDraft) => void;
  value: unknown;
}) {
  const { t } = useTranslation("settings");
  const draft = normalizeByteSizeDraft(value);
  const [localUnit, setLocalUnit] = useState(draft.unit);
  // Workspace and exact-format drafts carry their preferred unit. Persisted
  // family values are plain bytes, so retain the user's local unit while they
  // clear and retype the amount instead of unexpectedly switching to Bytes.
  const unit = isByteSizeDraft(value) ? draft.unit : localUnit;
  const amount = byteSizeAmountForUnit(draft.bytes, unit);

  function updateAmount(nextAmount: string) {
    onChange({
      bytes: bytesFromAmountAndUnit(nextAmount, unit),
      unit,
    });
  }

  function updateUnit(nextUnit: ByteSizeUnitValue) {
    setLocalUnit(nextUnit);
    onChange({
      bytes: draft.bytes,
      unit: nextUnit,
    });
  }

  return (
    <div className="upload-limit-control byte-size-editor">
      <div className="unit-input-row">
        <label htmlFor={fieldId}>
          {t("policy.byteSize.amount", { defaultValue: "Amount" })}
          <input
            aria-label={label}
            disabled={busy}
            id={fieldId}
            inputMode="decimal"
            max={maxBytes === undefined ? undefined : byteSizeAmountForUnit(maxBytes, unit)}
            min={byteSizeAmountForUnit(minBytes, unit)}
            onChange={(event) => updateAmount(event.target.value)}
            step={unit === "bytes" ? 1 : "any"}
            type="number"
            value={numberInputValue(amount)}
          />
        </label>
        <label>
          {t("policy.byteSize.unit", { defaultValue: "Unit" })}
          <select
            aria-label={t("policy.byteSize.unitLabel", {
              defaultValue: "{{label}} unit",
              label,
            })}
            disabled={busy}
            onChange={(event) => updateUnit(event.target.value as ByteSizeUnitValue)}
            value={unit}
          >
            {BYTE_SIZE_UNITS.map((unit) => (
              <option key={unit.value} value={unit.value}>
                {unit.label}
              </option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}

export function normalizeByteSizeDraft(value: unknown): ByteSizeDraft {
  if (isByteSizeDraft(value)) {
    return {
      bytes: Number(value.bytes),
      unit: byteSizeUnit(value.unit),
    };
  }
  const bytes = Number(value);
  return {
    bytes,
    unit: preferredByteSizeUnit(bytes),
  };
}

export function byteSizeDraftBytes(value: unknown) {
  return Math.round(normalizeByteSizeDraft(value).bytes);
}

function isByteSizeDraft(value: unknown): value is ByteSizeDraft {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return "bytes" in value && "unit" in value;
}

function byteSizeUnit(value: unknown): ByteSizeUnitValue {
  if (value === "gib" || value === "mb" || value === "kb" || value === "bytes") return value;
  return "mb";
}

function preferredByteSizeUnit(bytes: number): ByteSizeUnitValue {
  if (!Number.isFinite(bytes)) return "mb";
  if (bytes >= 1_073_741_824) return "gib";
  if (bytes >= 1_000_000) return "mb";
  if (bytes >= 1_000) return "kb";
  return "bytes";
}

function byteSizeAmountForUnit(bytes: number, unit: ByteSizeUnitValue) {
  return bytes / byteSizeMultiplier(unit);
}

function bytesFromAmountAndUnit(amount: string, unit: ByteSizeUnitValue) {
  const parsed = Number(amount);
  if (!Number.isFinite(parsed)) return Number.NaN;
  return Math.round(parsed * byteSizeMultiplier(unit));
}

function byteSizeMultiplier(unit: ByteSizeUnitValue) {
  return BYTE_SIZE_UNITS.find((item) => item.value === unit)?.multiplier ?? 1;
}

function numberInputValue(value: number) {
  if (!Number.isFinite(value)) return "";
  if (Number.isInteger(value)) return String(value);
  return String(Number(value.toFixed(3)));
}
