import { Inbox } from "lucide-react";

import { Breadcrumbs } from "../../shared/components/Breadcrumbs";

type WorkspacePlaceholderProps = {
  title: string;
  emptyLabel: string;
};

export function WorkspacePlaceholder({
  title,
  emptyLabel,
}: WorkspacePlaceholderProps) {
  return (
    <div className="workspace-screen single-column">
      <section className="feature-main">
        <Breadcrumbs items={[{ label: title }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">Workspace</p>
            <h1>{title}</h1>
          </div>
        </div>
        <div className="placeholder-panel">
          <Inbox size={22} aria-hidden="true" />
          <h2>{emptyLabel}</h2>
        </div>
      </section>
    </div>
  );
}
