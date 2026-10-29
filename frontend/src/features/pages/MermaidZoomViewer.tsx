import { useTranslation } from "react-i18next";

import { ZoomPanViewer } from "../../shared/components/ZoomPanViewer";

type Props = {
  svg: string;
  onClose: () => void;
};

export function MermaidZoomViewer({ svg, onClose }: Props) {
  const { t } = useTranslation("pages");
  return (
    <ZoomPanViewer
      closeLabel={t("mermaid.zoom.close", {
        defaultValue: "Close Mermaid zoom",
      })}
      contentLabel={t("mermaid.diagram", { defaultValue: "Mermaid diagram" })}
      controlsLabel={t("mermaid.zoom.controls", {
        defaultValue: "Mermaid zoom controls",
      })}
      onClose={onClose}
    >
      <div className="mermaid-zoom-svg" dangerouslySetInnerHTML={{ __html: svg }} />
    </ZoomPanViewer>
  );
}
