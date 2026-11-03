import { useTranslation } from "react-i18next";
import { Link, useInRouterContext } from "react-router-dom";
import type { ReferenceView } from "../../shared/api/references";
import { withContextHeadingFragment } from "../../shared/lib/contextLinks";
import { kindLabel, referenceKey } from "./documentReferences";

export function ReferencesSection({ references, anchorPrefix = "dtx-reference", checkingLocators }: {
  references: ReferenceView[];
  anchorPrefix?: string;
  checkingLocators?: ReadonlySet<string>;
}) {
  const { t } = useTranslation("pages");
  const inRouter = useInRouterContext();
  if (!references.length) return null;
  return (
    <section aria-label={t("references.title")} className="document-references">
      <h2>{t("references.title")}</h2>
      <ol>
        {references.map((reference) => (
          <li id={`user-content-${anchorPrefix}-${reference.number}`} key={referenceKey(reference)} value={reference.number}>
            {reference.state === "unavailable" ? (
              <span>{t(reference.occurrences?.some((item) => item.href && checkingLocators?.has(item.href)) ? "checkingReference.label" : "references.unavailable")}</span>
            ) : (
              <>
                {reference.href ? inRouter && reference.href.startsWith("/") && !reference.href.startsWith("//")
                  ? <Link to={withContextHeadingFragment(reference.href)}>{reference.title ?? t("references.unavailable")}</Link>
                  : <a href={withContextHeadingFragment(reference.href)}>{reference.title ?? t("references.unavailable")}</a> : <span>{reference.title}</span>}
                <span className="reference-kind">{t(`references.kinds.${kindLabel(reference.target_kind)}`)}</span>
                {reference.location ? <span className="reference-location">{reference.location}</span> : null}
                {reference.note ? <span className="reference-note">{reference.note}</span> : null}
                {reference.state === "unpublished" ? <span>{t("references.unpublished")}</span> : null}
                {reference.state === "deleted" ? <span>{t("references.deleted")}</span> : null}
                {reference.location_warning ? <span>{t("references.fragmentUnavailable")}</span> : null}
              </>
            )}
            {reference.inline_count === 0 ? <span className="reference-unused">{t("references.notCited")}</span> : null}
            {(reference.occurrences ?? []).map((_occurrence, index) => (
              <a aria-label={t("references.backToCitation", { number: reference.number, occurrence: index + 1 })}
                className="reference-backlink" href={`#user-content-${anchorPrefix}-${reference.number}-use-${index + 1}`}
                key={index}>↩{(reference.occurrences?.length ?? 0) > 1 ? String.fromCharCode(97 + index) : ""}</a>
            ))}
          </li>
        ))}
      </ol>
    </section>
  );
}
