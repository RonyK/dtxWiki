import { X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Dialog } from "./Dialog";

export type KeyboardShortcutGroup = {
  items: Array<{
    action: string;
    keys: string[];
  }>;
  label: string;
};

type KeyboardShortcutsModalProps = {
  groups: KeyboardShortcutGroup[];
  onClose: () => void;
};

export function KeyboardShortcutsModal({
  groups,
  onClose,
}: KeyboardShortcutsModalProps) {
  const { t } = useTranslation("pages");
  return (
    <Dialog
      className="editor-shortcut-dialog"
      labelledBy="keyboard-shortcuts-title"
      onClose={onClose}
    >
      <header className="editor-shortcut-dialog-header">
        <div>
          <p className="eyebrow">
            {t("editor.shortcutsModalEyebrow", { defaultValue: "Editor" })}
          </p>
          <h2 id="keyboard-shortcuts-title">
            {t("editor.shortcutsModalTitle", { defaultValue: "Keyboard shortcuts" })}
          </h2>
        </div>
        <button
          aria-label={t("editor.shortcutsModalCloseAria", {
            defaultValue: "Close keyboard shortcuts",
          })}
          className="icon-button"
          onClick={onClose}
          type="button"
        >
          <X size={16} aria-hidden="true" />
        </button>
      </header>
      <div className="editor-shortcut-grid">
        {groups.map((group) => (
          <section className="editor-shortcut-group" key={group.label}>
            <h3>{group.label}</h3>
            <dl>
              {group.items.map((item) => (
                <div
                  className="editor-shortcut-row"
                  key={`${group.label}-${item.action}`}
                >
                  <dt>{item.action}</dt>
                  <dd>
                    {item.keys.map((key) => (
                      <kbd key={key}>{key}</kbd>
                    ))}
                  </dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </Dialog>
  );
}
