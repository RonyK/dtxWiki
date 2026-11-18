import { Link2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";

import { Breadcrumbs } from "../../shared/components/Breadcrumbs";

/**
 * `/p/<page_id>` is an MCP shorthand that saves tokens; page bodies store it
 * as `/p/<workspace_id>/<page_id>`. A browser cannot know the workspace, so
 * this notice names the full form instead of guessing one.
 */
export function ShortPermalinkNotice() {
  const { t } = useTranslation("shell");
  const { pageId = "" } = useParams();
  return (
    <div className="workspace-screen single-column">
      <section className="feature-main">
        <Breadcrumbs items={[{ label: t("shortPermalink.breadcrumb") }]} />
        <div className="empty-state empty-state-rich route-not-found">
          <Link2 size={22} aria-hidden="true" />
          <h1>{t("shortPermalink.title")}</h1>
          <p>{t("shortPermalink.body")}</p>
          <p>
            <code className="short-permalink-form">{`/p/<workspace_id>/${pageId || "<page_id>"}`}</code>
          </p>
          <div className="empty-state-actions">
            <Link className="icon-button text-button primary" to="/workspaces/find">
              {t("workspace.find", { defaultValue: "Find workspaces" })}
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}
