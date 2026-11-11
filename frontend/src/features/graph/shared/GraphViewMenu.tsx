import { Eye } from "lucide-react";
import type { ReactNode, Ref } from "react";
import { useTranslation } from "react-i18next";

/** "View": settings that only change the drawing, in both graphs. */
export function GraphViewMenu({ children, ref }: { children: ReactNode; ref?: Ref<HTMLDetailsElement> }) {
  const { t } = useTranslation("graph");
  return (
    <details className="graph-view-options" ref={ref}>
      <summary><Eye aria-hidden="true" size={16} /><span>{t("graph.display.menu")}</span></summary>
      <div className="graph-view-panel">{children}</div>
    </details>
  );
}
