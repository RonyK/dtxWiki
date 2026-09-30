import { useState } from "react";

import type { TreeNodePayload } from "../api/workspace";
import { AvatarWithTimestamp } from "./Avatar";
import { ChipInput } from "./ChipInput";
import { CodeBlockWrapper } from "./CodeBlockWrapper";
import { DiffViewer } from "./DiffViewer";
import { EmojiPicker } from "./EmojiPicker";
import { FAB } from "./FAB";
import { FormField } from "./FormField";
import { BottomSheet } from "./BottomSheet";
import { KeyboardShortcutsModal } from "./KeyboardShortcutsModal";
import { MobileDrawer } from "./MobileDrawer";
import { NewItemDialog } from "./NewItemDialog";
import { TabbedInspector } from "./TabbedInspector";
import { TreeNodeContextMenu } from "./TreeNodeContextMenu";

const sampleNode: TreeNodePayload = {
  id: "context-node-1",
  section: "context",
  title: "Research Brief",
  slug: "research-brief",
  node_kind: "folder",
  parent_id: null,
  sort_key: "001",
  target_kind: null,
  target_id: null,
  is_system_node: false,
  can_have_children: true,
  primary_placement: true,
  visibility_status: "visible",
  children_count: 3,
  breadcrumb: [
    {
      id: "context-root",
      section: "context",
      title: "Context",
      slug: "context",
      node_kind: "folder",
    },
  ],
};

const diff = {
  from_version_no: 2,
  to_version_no: 3,
  additions: 2,
  deletions: 1,
  lines: [
    "--- v2",
    "+++ v3",
    "@@ -1,3 +1,4 @@",
    " # Research Brief",
    "-Old claim",
    "+Updated claim",
    "+New citation note",
  ],
};

function Sprint6ComponentInventory() {
  const [tags, setTags] = useState(["policy", "launch"]);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [menuAction, setMenuAction] = useState("No action yet");

  return (
    <div className="storybook-inventory">
      <section className="feature-section">
        <h2>Actor Identity</h2>
        <AvatarWithTimestamp
          actorType="human"
          name="Test User"
          timestamp="2026-05-24T10:00:00Z"
        />
      </section>

      <section className="feature-section">
        <h2>Form Foundation</h2>
        <FormField
          hint="Press Enter or comma to add a tag."
          htmlFor="story-tags"
          label="Tags"
        >
          <ChipInput
            ariaLabel="Tags"
            id="story-tags"
            onChange={setTags}
            placeholder="research"
            suggestions={["research", "decision", "evidence"]}
            value={tags}
          />
        </FormField>
      </section>

      <section className="feature-section">
        <h2>Markdown Code</h2>
        <CodeBlockWrapper code={"const value = 1;\nconsole.log(value);"} language="TypeScript">
          <pre>
            <code>{`const value = 1;\nconsole.log(value);`}</code>
          </pre>
        </CodeBlockWrapper>
      </section>

      <section className="feature-section">
        <h2>History Diff</h2>
        <DiffViewer diff={diff} />
      </section>

      <section className="feature-section">
        <h2>Inspector Tabs</h2>
        <TabbedInspector
          ariaLabel="Page inspector"
          tabs={[
            { key: "properties", label: "Properties", content: <p>Decision page</p> },
            { key: "history", label: "History", badge: 3, content: <p>Three versions</p> },
            { key: "backlinks", label: "Backlinks", content: <p>Two linked pages</p> },
          ]}
        />
      </section>

      <section className="feature-section">
        <h2>Tree Actions</h2>
        <div className="story-menu-surface">
          <TreeNodeContextMenu
            canCopyLink
            canCreateFolder
            canCreatePage
            canDelete
            canMove
            canRename
            node={sampleNode}
            onAction={(action) => setMenuAction(action.type)}
            onClose={() => setMenuAction("closed")}
            position={{ left: 16, top: 16 }}
          />
        </div>
        <p className="field-hint">{menuAction}</p>
        <button className="icon-button primary" type="button" onClick={() => setDialogOpen(true)}>
          Open New Item Dialog
        </button>
        {dialogOpen ? (
          <NewItemDialog
            initialItemType="page"
            onClose={() => setDialogOpen(false)}
            onSubmit={() => setDialogOpen(false)}
            parentLabel="Context / Research"
          />
        ) : null}
      </section>

      <section className="feature-section">
        <h2>Emoji Picker</h2>
        <EmojiPicker
          onClose={() => undefined}
          onSelect={(emoji) => setMenuAction(`selected ${emoji}`)}
          style={{ position: "relative", left: 0, top: 0 }}
        />
      </section>

      <section className="feature-section">
        <h2>Mobile Surfaces</h2>
        <MobileDrawer label="Explorer" mobileLayout mobileOpen>
          <button className="icon-button text-button" type="button">
            Drawer action
          </button>
        </MobileDrawer>
        <BottomSheet label="Details" mobileLayout mobileOpen>
          <button className="icon-button text-button" type="button">
            Sheet action
          </button>
        </BottomSheet>
        <button
          className="icon-button text-button"
          onClick={() => setShortcutsOpen(true)}
          type="button"
        >
          Keyboard shortcuts
        </button>
      </section>

      {shortcutsOpen ? (
        <KeyboardShortcutsModal
          groups={[
            {
              label: "Navigation",
              items: [{ action: "Show this panel", keys: ["?"] }],
            },
          ]}
          onClose={() => setShortcutsOpen(false)}
        />
      ) : null}

      <FAB ariaLabel="Create Context page" label="New" onClick={() => setDialogOpen(true)} />
    </div>
  );
}

const meta = {
  title: "Shared/Sprint 6 Component Inventory",
  component: Sprint6ComponentInventory,
};

export default meta;

export const Inventory = {};
