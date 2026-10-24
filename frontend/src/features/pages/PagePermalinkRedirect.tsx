import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useLocation, useParams } from "react-router-dom";

import { ApiError, apiErrorMessage } from "../../shared/api/client";
import {
  getContextPageLocation,
  type ContextPageLocation,
} from "../../shared/api/operations";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { NotFoundScreen } from "../errors/NotFoundScreen";

type Resolution = {
  pageId: string;
} & (
  | { location: ContextPageLocation }
  | { notFound: true }
  | { error: string }
);

export function PagePermalinkRedirect() {
  const { t } = useTranslation("pages");
  const { pageId = "" } = useParams();
  const { hash } = useLocation();
  const [resolution, setResolution] = useState<Resolution | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getContextPageLocation(pageId)
      .then((location) => {
        if (!cancelled) setResolution({ pageId, location });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (
          error instanceof ApiError &&
          (error.status === 404 ||
            error.status === 422 ||
            error.body.error_code === "invalid_request_path")
        ) {
          setResolution({ pageId, notFound: true });
        } else {
          setResolution({
            pageId,
            error: apiErrorMessage(error, t("reader.failedLoad")),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [pageId, t]);

  if (!resolution || resolution.pageId !== pageId) {
    return <RouteLoading label={t("reader.loading")} />;
  }
  if ("notFound" in resolution) return <NotFoundScreen />;
  if ("error" in resolution) {
    return <StatusBanner tone="warning">{resolution.error}</StatusBanner>;
  }
  const { workspace_id, page_id } = resolution.location;
  return (
    <Navigate
      replace
      to={`/workspaces/${encodeURIComponent(workspace_id)}/context/pages/${encodeURIComponent(page_id)}${hash}`}
    />
  );
}
