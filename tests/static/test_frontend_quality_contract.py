import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STYLES_DIR = ROOT / "frontend/src/styles"


def read_app_styles() -> str:
    index = (STYLES_DIR / "index.css").read_text(encoding="utf-8")
    partials = re.findall(r'@import\s+"\./([^"]+)";', index)
    if not partials:
        return index
    return "\n".join(
        (STYLES_DIR / partial).read_text(encoding="utf-8")
        for partial in partials
    )


def test_sprint3_async_load_hook_is_used_by_route_screens():
    feature_root = ROOT / "frontend/src/features"
    hook_uses = [
        path
        for path in feature_root.rglob("*Screen.tsx")
        if "useAsyncLoad" in path.read_text(encoding="utf-8")
    ]

    assert len(hook_uses) == 12
    assert any(path.name == "SearchScreen.tsx" for path in hook_uses)


def test_semantic_oklch_palette_and_semantic_classes_are_defined():
    stylesheet = read_app_styles()

    assert not (ROOT / "frontend/tailwind.config.ts").exists()

    for token in [
        "--status-ok",
        "--status-warn",
        "--status-danger",
        "--status-info",
        "--ink-3",
        "--ink-5",
        "--paper",
        # Lifecycle dots reuse the identity hue tokens so Library reads teal,
        # not amber (C3 / audit A45) — kept in sync with index.css :63-71.
        "#0f9f8f",
        "#7c3aed",
        "oklch(0.72 0.15 145)",
    ]:
        assert token in stylesheet

    for class_name in [
        ".pill-ok",
        ".pill-warn",
        ".pill-danger",
        ".pill-muted",
        ".pill-info",
        ".lc-dot-library",
        ".lc-dot-context",
        ".lc-dot-agent",
        ".card",
    ]:
        assert class_name in stylesheet
    assert re.search(r"^\s*@apply\b", stylesheet, flags=re.M) is None


def test_shared_components_barrel_export_is_exhaustive():
    """Every `*.tsx` source component must be re-exported from `index.ts`.

    `.test.tsx` and `.stories.tsx` files are excluded because they are not
    public exports of the design system. Adding a new component without
    wiring it into the barrel will fail this guard.
    """
    components_dir = ROOT / "frontend/src/shared/components"
    barrel_path = components_dir / "index.ts"
    assert barrel_path.exists(), (
        "frontend/src/shared/components/index.ts is missing — "
        "create a barrel that re-exports every component module"
    )

    component_files = sorted(
        path
        for path in components_dir.glob("*.tsx")
        if not path.name.endswith(".test.tsx")
        and not path.name.endswith(".stories.tsx")
    )
    assert component_files, "expected at least one component to barrel"

    barrel_text = barrel_path.read_text(encoding="utf-8")
    # Permit `export * from "./Name";` or `export { ... } from "./Name";`.
    exported_modules = set(
        re.findall(r'^\s*export[^\n]*from\s+"\./([^"]+)";', barrel_text, re.M)
    )

    missing = [
        path.stem for path in component_files if path.stem not in exported_modules
    ]
    assert not missing, (
        "shared/components/index.ts must re-export every component module; "
        f"missing: {missing}"
    )


def test_workspace_scroll_surfaces_remain_discoverable():
    stylesheet = read_app_styles()

    feature_main = declarations_for_selector(stylesheet, ".feature-main")
    tree_rail = declarations_for_selector(stylesheet, ".tree-rail")
    tree_rail_group_bodies = selector_bodies(stylesheet, ".tree-rail")
    scrollbar = declarations_for_selector(
        stylesheet,
        ".tree-rail-body::-webkit-scrollbar",
    )
    scrollbar_group = declarations_for_selector(stylesheet, ".tree-rail-body")

    assert feature_main["overflow-x"] == "hidden"
    assert feature_main["overflow-y"] == "auto"
    assert tree_rail["display"] == "flex"
    assert tree_rail["flex-direction"] == "column"
    assert tree_rail["min-width"] == "0"
    assert tree_rail["min-height"] == "0"
    assert all("overflow: hidden" not in body for body in tree_rail_group_bodies)
    assert scrollbar["width"] == "8px"
    assert scrollbar_group["scrollbar-width"] == "thin"
    assert scrollbar_group["scrollbar-color"] == "var(--border-strong) transparent"


def test_keyboard_shortcut_dialog_keeps_wide_layout_contract():
    stylesheet = read_app_styles()

    shortcut_dialog = declarations_for_selector(
        stylesheet,
        ".dialog-panel.editor-shortcut-dialog",
    )
    shortcut_labels = declarations_for_selector(
        stylesheet,
        ".editor-shortcut-row dt",
    )

    assert shortcut_dialog["width"] == "min(960px, 100%)"
    assert shortcut_dialog["max-height"] == "min(760px, calc(100vh - 40px))"
    assert shortcut_dialog["overflow"] == "auto"
    assert shortcut_labels["overflow-wrap"] == "break-word"
    assert shortcut_labels["word-break"] == "normal"


def test_topbar_keeps_workspace_switcher_separate_from_search_track():
    stylesheet = read_app_styles()

    topbar_bodies = selector_bodies(stylesheet, ".topbar")
    collapsed_topbar_bodies = selector_bodies(
        stylesheet,
        '.app-shell[data-sidebar-collapsed="true"] .topbar',
    )
    search = declarations_for_selector(stylesheet, ".topbar-search")
    actions = declarations_for_selector(stylesheet, ".topbar-actions")

    expanded_columns = (
        "grid-template-columns: minmax(var(--sidebar-w), max-content) "
        "minmax(280px, 620px) minmax(0, 1fr);"
    )
    collapsed_columns = (
        "grid-template-columns: "
        "minmax(var(--sidebar-collapsed-w), max-content) "
        "minmax(240px, 620px) minmax(0, 1fr);"
    )
    assert any(
        expanded_columns in body and "column-gap: 12px;" in body
        for body in topbar_bodies
    )
    assert any(
        collapsed_columns in " ".join(body.split())
        for body in collapsed_topbar_bodies
    )
    collapsed_switcher_bodies = selector_bodies(
        stylesheet,
        '.app-shell[data-sidebar-collapsed="true"] .workspace-switcher-button',
    )
    assert any(
        "grid-template-columns: 22px 14px;" in body
        for body in collapsed_switcher_bodies
    )
    assert search["margin"] == "0"
    assert search["justify-self"] == "stretch"
    assert actions["justify-self"] == "end"
    assert actions["justify-content"] == "flex-end"
    assert actions["min-width"] == "max-content"


def test_editor_title_tags_only_claim_wide_space_when_chips_exist():
    stylesheet = read_app_styles()

    empty_tags = declarations_for_selector(stylesheet, ".editor-title-tags")
    populated_tags = declarations_for_selector(
        stylesheet,
        ".editor-title-tags:has(.chip-input-chip)",
    )

    assert empty_tags["flex"] == "0 0 auto"
    assert empty_tags["min-width"] == "80px"
    # L-3 (260908 UI audit): a fixed 320px flex-basis reserved that width even
    # for a single short tag chip, leaving a large empty box. Size to content
    # (flex-grow/shrink still bounded by max-width) instead of a fixed basis.
    assert populated_tags["flex"] == "0 1 auto"
    assert populated_tags["max-width"] == "320px"


def test_page_properties_popover_uses_dynamic_anchor_position():
    stylesheet = read_app_styles()
    editor_source = (ROOT / "frontend/src/features/pages/PageEditor.tsx").read_text(
        encoding="utf-8"
    )

    popover = declarations_for_selector(stylesheet, ".editor-properties-popover")

    assert popover["position"] == "fixed"
    assert popover["top"] == "0"
    assert popover["left"] == "0"
    assert "right" not in popover
    assert 'placement: "bottom-end"' in editor_source
    assert "propertiesPopoverPosition" in editor_source


def test_mobile_ask_composer_remains_sticky_in_ask_surface():
    stylesheet = read_app_styles()

    composer = declarations_for_selector(stylesheet, ".ask-screen .ask-composer")
    # H-3 (260908 UI audit): the shared `.workspace-screen[data-mobile-action-
    # reserve="true"] .feature-main` rule below is (0,3,0) specificity, so a
    # plain `.ask-screen .feature-main` (0,2,0) silently lost to it -- no
    # padding-bottom this test set here ever reached the page. The selector
    # was raised to match specificity (two classes on the screen root + one on
    # .feature-main) so Ask can override it without touching every other
    # screen that shares that reservation.
    feature_main = declarations_for_selector(
        stylesheet, ".workspace-screen.ask-screen .feature-main"
    )
    empty_feature_main = declarations_for_selector(
        stylesheet,
        ".ask-screen .feature-main.ask-empty",
    )
    empty_composer = declarations_for_selector(
        stylesheet,
        ".ask-screen .feature-main.ask-empty .ask-composer",
    )
    mobile_actions = declarations_for_selector(
        stylesheet,
        ".ask-screen .workspace-mobile-actions",
    )
    reserved_workspace_main = declarations_for_selector(
        stylesheet,
        '.workspace-screen[data-mobile-action-reserve="true"] .feature-main',
    )

    assert reserved_workspace_main["padding-bottom"] == (
        "calc(180px + env(safe-area-inset-bottom))"
    )
    assert composer["position"] == "sticky"
    assert composer["bottom"] == "0"
    assert composer["z-index"] == "60"
    assert composer["background"] == "var(--surface)"
    # H-3 (260908 UI audit): a sticky-bottom element's stuck position moves UP
    # by exactly its container's own padding-bottom (it sticks to the padding
    # edge, not the border edge). The old 24px (and the 150-230px it evolved
    # from) never closed the gap between the composer and the last turn card
    # -- measured overlap plateaus at a constant 13.75px for any value from
    # 48px to 180px, while 0 measured an 18px clearance instead. Only the
    # safe-area inset is genuinely needed here (notch clearance).
    assert feature_main["padding-bottom"] == "env(safe-area-inset-bottom)"
    assert ".ask-composer-reserve" not in stylesheet
    assert mobile_actions["bottom"] == "calc(76px + env(safe-area-inset-bottom))"
    assert mobile_actions["background"] == "var(--surface)"
    assert mobile_actions["border"] == "1px solid var(--border)"
    assert empty_feature_main["padding-bottom"] == (
        "calc(24px + env(safe-area-inset-bottom))"
    )
    assert empty_composer["position"] == "static"
    assert empty_composer["box-shadow"] == "none"


def test_oauth_consents_empty_state_routes_authorization_to_app_hub():
    """Keep OAuth authorization discoverable without inline app-specific commands."""
    screen = (ROOT / "frontend/src/features/settings/SettingsScreen.tsx").read_text(
        encoding="utf-8"
    )

    assert "No OAuth apps are authorized yet." in screen
    assert "<McpAppHub" in screen
    assert "loadMcpSetupPlan" in screen
    assert "Access keys" in screen


def test_library_drag_drop_upload_surface_has_overlay_contract():
    stylesheet = read_app_styles()
    library_screen = (
        ROOT / "frontend/src/features/library/LibraryScreen.tsx"
    ).read_text(encoding="utf-8")

    feature_main = declarations_for_selector(stylesheet, ".feature-main")
    drag_over = declarations_for_selector(stylesheet, ".feature-main--drag-over")
    overlay = declarations_for_selector(stylesheet, ".upload-drop-overlay")

    assert feature_main["position"] == "relative"
    assert drag_over["outline"] == "2px dashed var(--library)"
    assert overlay["position"] == "absolute"
    assert overlay["pointer-events"] == "none"
    assert "onDragOver={handleDragOver}" in library_screen
    assert "onDrop={handleDrop}" in library_screen


def test_library_json_viewer_uses_structured_json_component():
    stylesheet = read_app_styles()
    source_screen = (
        ROOT / "frontend/src/features/library/LibraryScreen.tsx"
    ).read_text(encoding="utf-8")
    renderers = (
        ROOT / "frontend/src/features/library/preview/renderers.tsx"
    ).read_text(encoding="utf-8")
    legacy_renderers = (
        ROOT / "frontend/src/features/library/preview/LegacyPreviewRenderer.tsx"
    ).read_text(encoding="utf-8")
    package_json = (ROOT / "frontend/package.json").read_text(encoding="utf-8")

    viewer = declarations_for_selector(stylesheet, ".library-json-viewer")

    assert '"react-json-view-lite"' in package_json
    assert 'from "./preview/PreviewPane"' in source_screen
    assert 'from "react-json-view-lite"' in renderers
    assert "export function MetadataPreviewRenderer" in renderers
    assert "safeParseJson" in legacy_renderers
    assert viewer["overflow"] == "auto"
    assert viewer["font-family"].startswith("ui-monospace")


def test_library_pdf_viewer_uses_lazy_react_pdf_component():
    stylesheet = read_app_styles()
    source_screen = (
        ROOT / "frontend/src/features/library/LibraryScreen.tsx"
    ).read_text(encoding="utf-8")
    renderers = (
        ROOT / "frontend/src/features/library/preview/renderers.tsx"
    ).read_text(encoding="utf-8")
    pdf_viewer = (
        ROOT / "frontend/src/features/library/PdfLibraryPreview.tsx"
    ).read_text(encoding="utf-8")
    package_json = (ROOT / "frontend/package.json").read_text(encoding="utf-8")

    viewer = declarations_for_selector(stylesheet, ".library-pdf-viewer")
    document = declarations_for_selector(stylesheet, ".library-pdf-document")

    assert '"react-pdf"' in package_json
    assert 'from "react-pdf"' in pdf_viewer
    assert "pdf.worker.min.mjs" in pdf_viewer
    assert 'from "./preview/PreviewPane"' in source_screen
    assert "const PdfLibraryPreview = lazy" in renderers
    assert "export function PdfPreviewRenderer" in renderers
    assert "transport.pdfSource" in renderers
    assert viewer["display"] == "grid"
    assert document["overflow"] == "auto"


def test_tree_row_inline_actions_clear_desktop_hit_area_floor():
    stylesheet = read_app_styles()

    rail_bodies = selector_bodies(stylesheet, ".tree-rail")
    action_bodies = selector_bodies(stylesheet, ".tree-row-add")

    assert any("--tree-control-hit-size: 28px;" in body for body in rail_bodies)
    assert any("--tree-control-hit-size: 44px;" in body for body in rail_bodies)
    assert any(
        "width: var(--tree-control-hit-size);" in body
        and "height: var(--tree-control-hit-size);" in body
        and "min-width: var(--tree-control-hit-size);" in body
        and "min-height: var(--tree-control-hit-size);" in body
        for body in action_bodies
    )


def test_tree_row_actions_overlay_rather_than_reserve_a_column():
    """Idle rail rows spend their full width on the title.

    A reserved trailing grid track costs --tree-control-hit-size on every row
    (28px desktop, 44px touch) to hold a control that is only ever visible on
    the single row the pointer is over. Measured on a 280px rail that is 35px,
    or 17% of the title track, and it forces an ellipsis onto titles that would
    otherwise fit. The overlay treatment was implemented once and then silently
    dropped by a later hit-target pass, which is why both halves are pinned
    here: three tracks, and an absolutely positioned action cluster whose own
    box still clears the hit-area floor asserted above.
    """

    stylesheet = read_app_styles()

    for body in selector_bodies(stylesheet, ".tree-row"):
        if "grid-template-columns" not in body:
            continue
        tracks = body.split("grid-template-columns:")[1].split(";")[0]
        assert "max-content" not in tracks, (
            "the trailing action track is back; hover actions must overlay"
        )
        assert tracks.strip().endswith("minmax(0, 1fr)"), (
            f"the title track must be last and elastic, got: {tracks.strip()}"
        )

    actions = declarations_for_selector(stylesheet, ".tree-row-actions")
    assert actions["position"] == "absolute"
    assert actions["inset-inline-end"] == "4px"
    assert "grid-column" not in actions
    assert actions["background"] == "var(--tree-row-action-surface)"

    fade = declarations_for_selector(stylesheet, ".tree-row-actions::before")
    assert fade["width"] == "var(--tree-row-action-fade)"
    assert "var(--tree-row-action-surface)" in fade["background"]
    # Padding on the cluster itself would inflate the box the responsive
    # contract measures against the 48px touch ceiling, so the fade has to stay
    # outside it.
    assert "padding" not in actions
    assert "padding-inline-start" not in actions

    # The trailing badges occupy exactly the band the overlay covers; without
    # this the count pill is caught half-way through the fade.
    badges = declarations_for_selector(
        stylesheet,
        ".tree-row-label:has(~ .tree-row-actions) > .tree-row-child-count",
    )
    assert badges["opacity"] == "var(--tree-row-badge-opacity, 1)"


def test_tree_row_chevron_hover_clears_the_selection_ring():
    """The fold arrow's hover tint must not paint over the selected row's ring.

    The chevron's hit target is wider than the slot it sits in, so its negative
    inline margin pulls it out to the row's own edge on a level-1 row -- exactly
    where `.tree-row.active` paints its 1px inset ring. A child background
    covers a parent's inset box-shadow, so an edge-to-edge tint eats the ring.

    The inset has to be expressed with the same tokens as the margin that
    causes the overhang, otherwise touch (44px hit in a 32px slot) needs 6px
    while desktop needs 2px and the two drift apart. It also has to stay
    padding rather than a smaller width: the hit target itself is pinned at
    28/44px by tests/e2e/overall_ui_responsive.spec.ts.
    """

    stylesheet = read_app_styles()
    chevron = declarations_for_selector(stylesheet, ".tree-row-chevron")

    assert chevron["background-clip"] == "content-box", (
        "the chevron hover tint must be clipped to the content box or it "
        "repaints the selected row's leading ring"
    )

    inset = chevron["padding-inline"].replace(" ", "").replace("\n", "")
    assert inset == (
        "calc((var(--tree-control-hit-size)-var(--tree-leading-visual-slot))/2)"
    ), (
        "the tint inset must be derived from the same tokens as the overhang, "
        f"got: {chevron['padding-inline']}"
    )

    # A width shrunk to the slot would look identical and silently break the
    # touch hit-target floor, so the box must still be the full hit size.
    # Only `width` is checked: the helper merges every rule matching this
    # selector, and the mobile block in 07-settings-policy.css legitimately
    # raises min-width to a literal 44px.
    assert chevron["width"] == "var(--tree-control-hit-size)"

    hover = declarations_for_selector(stylesheet, ".tree-row-chevron:hover")
    assert hover.get("background-clip") == "content-box", (
        "the hover rule must restate background-clip: content-box. It sets the "
        "`background` shorthand, which resets every background-* longhand, so "
        "dropping this line silently returns the tint to border-box and the "
        "selected row loses its leading ring again."
    )


def test_tree_rail_drag_drop_move_contract():
    stylesheet = read_app_styles()
    tree_rail_source = (
        ROOT / "frontend/src/shared/components/TreeRail.tsx"
    ).read_text(encoding="utf-8")
    context_source = (
        ROOT / "frontend/src/features/context/ContextScreen.tsx"
    ).read_text(encoding="utf-8")
    agent_source = (ROOT / "frontend/src/features/agent/AgentScreen.tsx").read_text(
        encoding="utf-8"
    )
    library_source = (
        ROOT / "frontend/src/features/library/LibraryScreen.tsx"
    ).read_text(encoding="utf-8")

    drop_target = declarations_for_selector(
        stylesheet,
        ".tree-row.tree-row-drop-target",
    )

    edit_gated_drop = "onDropNode={canEdit ? moveTreeNodeByDrop : undefined}"
    assert "const TREE_NODE_DRAG_DATA" in tree_rail_source
    assert "onDropNode" in tree_rail_source
    assert "isTreeDescendant(orderedNodes" in tree_rail_source
    assert 'targetNode.id !== draggedNode.parent_id' in tree_rail_source
    assert edit_gated_drop in context_source
    assert edit_gated_drop in agent_source
    assert edit_gated_drop in library_source
    assert drop_target["box-shadow"].startswith("inset 0 0 0 2px")


def test_workspace_tree_rail_default_width_leaves_header_room():
    stylesheet = read_app_styles()
    layout_source = (
        ROOT / "frontend/src/shared/components/WorkspaceLayout.tsx"
    ).read_text(encoding="utf-8")

    workspace_screen = declarations_for_selector(stylesheet, ".workspace-screen")
    ask_screen = declarations_for_selector(stylesheet, ".workspace-screen.ask-screen")

    assert workspace_screen["--workspace-left-w"] == "280px"
    assert ask_screen["--workspace-left-w"] == "300px"
    assert "const DEFAULT_LEFT_WIDTHS = [280, 420, 560] as const;" in layout_source
    assert "const ASK_LEFT_WIDTHS = [300, 420, 560] as const;" in layout_source


def test_workspace_explorer_rails_default_open_on_browse_surfaces():
    context = (ROOT / "frontend/src/features/context/ContextScreen.tsx").read_text(
        encoding="utf-8"
    )
    agent = (ROOT / "frontend/src/features/agent/AgentScreen.tsx").read_text(
        encoding="utf-8"
    )
    sources = (ROOT / "frontend/src/features/library/LibraryScreen.tsx").read_text(
        encoding="utf-8"
    )
    layout_source = (
        ROOT / "frontend/src/shared/components/WorkspaceLayout.tsx"
    ).read_text(encoding="utf-8")

    assert "defaultLeftOpen?: boolean;" in layout_source
    assert "defaultLeftOpen = true," in layout_source
    assert "readStoredPanelState(`${layoutKey}:left`, defaultLeftOpen)" in layout_source
    assert "defaultLeftOpen" in agent
    assert "defaultLeftOpen={false}" not in agent
    assert "defaultLeftOpen" not in context
    assert "defaultLeftOpen" not in sources


def test_keyboard_accessibility_chrome_contract():
    shell = (ROOT / "frontend/src/shared/components/Shell.tsx").read_text(
        encoding="utf-8"
    )
    stylesheet = read_app_styles()

    assert 'className="skip-link"' in shell
    assert 'href="#main-content"' in shell
    assert 'id="main-content"' in shell
    assert 'tabIndex={-1}' in shell
    assert ".skip-link:focus-visible" in stylesheet
    assert ":focus-visible" in stylesheet
    assert "outline: 2px solid var(--info)" in stylesheet
    assert "button.metric-card:focus-visible" in stylesheet
    stale_metric_focus = (
        "button.metric-card:focus-visible {\n"
        "  border-color: var(--agent);\n"
        "  background: var(--agent-soft);\n"
        "  outline: none;"
    )
    assert stale_metric_focus not in stylesheet


def test_no_button_renders_without_a_visible_treatment():
    """A `<button>` with no class is invisible, not merely unstyled.

    `01-base.css` resets buttons to a transparent, border-less, padding-less
    box, so an unclassed button renders as bare body text. That shipped on the
    Context pending-review strip, where "Approve" and "Send request" — the two
    controls that accept or reject a draft — looked like paragraph text.

    A button may still omit a class when a parent selector styles it (the
    editor popovers and chip inputs do this deliberately), so the allowlist
    below records those containers. Anything new must either carry a button
    class or be added here with its parent rule.
    """

    parent_styled_containers = {
        "editor-link-header",
        "editor-link-actions",
        "editor-comment-header",
        "editor-comment-actions",
        "empty-suggestions",
        "chip-input-chip",
        "chip-input-suggestions",
        "diff-viewer-mode-toggle",
    }
    stylesheet = read_app_styles()
    for container in sorted(parent_styled_containers):
        assert f".{container} button" in stylesheet, (
            f"allowlisted container .{container} no longer styles its buttons; "
            "either restore the rule or give those buttons an explicit class"
        )

    offenders: list[str] = []
    for path in sorted((ROOT / "frontend/src").rglob("*.tsx")):
        if "/tests/" in path.as_posix() or path.name.endswith(".test.tsx"):
            continue
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<button\b", source):
            closing = source.find("</button>", match.end())
            end = closing if closing >= 0 else len(source)
            button_source = source[match.start() : end]
            if "className" in button_source:
                continue
            preceding = source[: match.start()]
            if any(
                f'"{container}"' in preceding[-2000:]
                for container in parent_styled_containers
            ):
                continue
            line = preceding.count("\n") + 1
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{line}")

    assert offenders == [], (
        "unclassed <button> renders as bare text under the base reset: "
        + ", ".join(offenders)
    )


def declarations_for_selector(stylesheet: str, selector: str) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for body in selector_bodies(stylesheet, selector):
        for name, value in re.findall(r"([a-z-]+)\s*:\s*([^;{}]+);", body):
            declarations[name] = value.strip()
    return declarations


def selector_bodies(stylesheet: str, selector: str) -> list[str]:
    bodies: list[str] = []
    decommented = re.sub(r"/\*.*?\*/", "", stylesheet, flags=re.S)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", decommented):
        selector_list = [entry.strip() for entry in match.group(1).split(",")]
        if selector in selector_list:
            bodies.append(match.group(2))
    assert bodies, f"selector not found: {selector}"
    return bodies


def test_heading_rows_pin_whatever_trails_the_title():
    """A bare control after the title must sit at the trailing edge too.

    `.feature-header` dropped `justify-content: space-between` so wrapped rows
    would not spread, and pinned the replacement on the two named action
    clusters. Six headers put a plain button, chip or pill there instead, and
    every one of them collapsed to 16px from the title with the rest of the row
    empty — the "why is this control in the middle of nowhere" report.
    """

    styles = read_app_styles()

    assert ".feature-header > :last-child:not(:first-child)" in styles
    assert ".detail-heading > :last-child:not(:first-child)" in styles

    # The screens that rely on it, so removing the pin cannot look harmless.
    bare_trailing_headers = {
        "features/ask/AskScreen.tsx",
        "features/lint/LintScreen.tsx",
        "features/search/SearchScreen.tsx",
        "features/mcp-chats/ChatsListScreen.tsx",
        "features/activity/ActivityScreen.tsx",
        "features/dashboard/DashboardScreen.tsx",
    }
    for relative in sorted(bare_trailing_headers):
        source = (ROOT / "frontend/src" / relative).read_text(encoding="utf-8")
        assert 'className="feature-header"' in source, relative


def test_the_content_band_leaves_the_upload_overlay_alone():
    """The overlay is positioned against `.feature-main` and marks the whole
    drop target, so banding it to 1200px paints the affordance somewhere the
    drop still works. The `--form` rule already excluded it; the default rule
    introduced later did not."""

    styles = read_app_styles()

    assert ".feature-main > :not(.upload-drop-overlay)" in styles
    assert ".feature-main > * {" not in styles
