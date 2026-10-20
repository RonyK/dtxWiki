import { useTranslation } from "react-i18next";

export function ActivityGroupingControl({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  const { t } = useTranslation("activity");
  return (
    <label className="activity-grouping-control">
      <input
        checked={checked}
        onChange={(event) => onChange(event.currentTarget.checked)}
        type="checkbox"
      />
      <span>{t("feed.groupReads", { defaultValue: "Group repeated reads" })}</span>
    </label>
  );
}
