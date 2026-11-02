import { useTranslation } from "react-i18next";
import type { ReferenceView } from "../api/references";

const referenceKey = (item: ReferenceView) => `${item.target_kind}:${item.target_id ?? item.metadata_id ?? `unavailable-${item.number}`}`;

export function ReferenceChanges({ before, after }: { before: ReferenceView[]; after: ReferenceView[] }) {
  const { t } = useTranslation("pages");
  const old = new Map(before.map((item) => [referenceKey(item), item]));
  const next = new Map(after.map((item) => [referenceKey(item), item]));
  const removed = before.filter((item) => !next.has(referenceKey(item)));
  const added = after.filter((item) => !old.has(referenceKey(item)));
  const changed = after.filter((item) => {
    const previous = old.get(referenceKey(item));
    return previous && ((previous.note ?? "") !== (item.note ?? "") || previous.metadata_present !== item.metadata_present);
  });
  if (!removed.length && !added.length && !changed.length) return null;
  return (
    <section aria-label={t("references.changes")} className="reference-changes">
      <h3>{t("references.changes")}</h3>
      <ul>
        {removed.map((item) => <li key={`removed:${referenceKey(item)}`}>− {t("references.changeRemoved")}: {item.title ?? t("references.unavailable")}</li>)}
        {added.map((item) => <li key={`added:${referenceKey(item)}`}>+ {t("references.changeAdded")}: {item.title ?? t("references.unavailable")}{item.note ? ` — ${item.note}` : ""}</li>)}
        {changed.map((item) => <li key={`changed:${referenceKey(item)}`}>{t("references.changeEdited")}: {item.title ?? t("references.unavailable")}<br />
          <del>{old.get(referenceKey(item))?.note}</del> <ins>{item.note}</ins>
        </li>)}
      </ul>
    </section>
  );
}
