"""Fixture tests for scripts/check-i18n-coverage.mjs (I18N-05).

The gate used to compare *key sets* only between en/ and ko/ locale JSON,
never values and never call sites. That let ko values stay byte-identical
to en (I18N-03) and let `t("key", { defaultValue: "English" })` calls
reference a key that does not exist in either locale (I18N-04) — both slip
through invisibly while `npm run check:i18n` prints green.

These tests build isolated fixture trees (never the real, actively-edited
frontend/src) and point the script at them via env vars, so each assertion
proves the gate actually reds out on the defect class it claims to catch —
not merely that the script runs without crashing.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-i18n-coverage.mjs"


def run_gate(locale_root: Path, src_root: Path, allowlist_path: Path):
    env = dict(os.environ)
    env["I18N_CHECK_LOCALE_ROOT"] = str(locale_root)
    env["I18N_CHECK_SRC_ROOT"] = str(src_root)
    env["I18N_CHECK_ALLOWLIST_PATH"] = str(allowlist_path)
    return subprocess.run(
        ["node", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def write_locales(tmp_path: Path, en: dict, ko: dict, filename: str = "test.json"):
    locale_root = tmp_path / "locales"
    (locale_root / "en").mkdir(parents=True)
    (locale_root / "ko").mkdir(parents=True)
    (locale_root / "en" / filename).write_text(
        json.dumps(en, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (locale_root / "ko" / filename).write_text(
        json.dumps(ko, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return locale_root


def write_allowlist(tmp_path: Path, entries: list[str]):
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def empty_src(tmp_path: Path) -> Path:
    src_root = tmp_path / "src"
    src_root.mkdir()
    return src_root


def test_gate_passes_on_a_clean_fixture(tmp_path):
    locale_root = write_locales(
        tmp_path,
        en={"greeting": "Hello"},
        ko={"greeting": "안녕하세요"},
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, empty_src(tmp_path), allowlist)
    assert result.returncode == 0, result.stderr
    assert "i18n coverage OK" in result.stdout


def test_gate_reds_out_when_a_ko_value_is_byte_identical_to_en(tmp_path):
    """I18N-03: the key exists in both locales, but the ko value was never
    translated. The old gate only diffed key *sets*, so this passed silently.
    """
    locale_root = write_locales(
        tmp_path,
        en={"nav": {"context": "Context"}},
        ko={"nav": {"context": "Context"}},
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, empty_src(tmp_path), allowlist)
    assert result.returncode != 0
    assert "nav.context is identical to en" in result.stderr


def test_gate_passes_when_the_identical_value_is_allowlisted(tmp_path):
    """A deliberate proper noun (e.g. "MCP") must not be forced to diverge."""
    locale_root = write_locales(
        tmp_path,
        en={"protocol": "MCP"},
        ko={"protocol": "MCP"},
    )
    allowlist = write_allowlist(tmp_path, ["test.json:protocol"])
    result = run_gate(locale_root, empty_src(tmp_path), allowlist)
    assert result.returncode == 0, result.stderr


def test_gate_reds_out_on_a_stale_allowlist_entry(tmp_path):
    """An allowlist entry whose ko value has since diverged from en (i.e. it
    got translated) is dead weight that should be removed — keeps the
    allowlist honest instead of silently growing forever.
    """
    locale_root = write_locales(
        tmp_path,
        en={"protocol": "MCP"},
        ko={"protocol": "MCP"},
    )
    allowlist = write_allowlist(
        tmp_path, ["test.json:protocol", "test.json:no.such.key"]
    )
    result = run_gate(locale_root, empty_src(tmp_path), allowlist)
    assert result.returncode != 0
    assert "test.json:no.such.key" in result.stderr


def test_gate_reds_out_on_an_orphan_defaultvalue_key(tmp_path):
    """I18N-04: `t("key", { defaultValue: "English" })` where "key" does not
    exist in en/ renders the English fallback forever. The old gate never
    inspected source files at all.
    """
    locale_root = write_locales(
        tmp_path,
        en={"list": {"draftIndicator": "Unpublished draft"}},
        ko={"list": {"draftIndicator": "게시되지 않은 초안"}},
    )
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "Screen.tsx").write_text(
        """
        import { useTranslation } from "react-i18next";
        export function Screen() {
          const { t } = useTranslation("test");
          return <span>{t("list.loadMore", { defaultValue: "Load more" })}</span>;
        }
        """,
        encoding="utf-8",
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, src_root, allowlist)
    assert result.returncode != 0
    assert 'list.loadMore' in result.stderr
    assert "en/test.json" in result.stderr


def test_gate_does_not_flag_an_inline_ns_override_bound_to_a_different_namespace(
    tmp_path,
):
    """The exact false-positive class from the I18N-04 review: a component's
    `useTranslation` binds one namespace, but a specific call overrides `ns`
    to a *different* namespace where the key does exist. A naive "does this
    file's bound namespace have this key" check would wrongly flag this.
    """
    locale_root = tmp_path / "locales"
    (locale_root / "en").mkdir(parents=True)
    (locale_root / "ko").mkdir(parents=True)
    (locale_root / "en" / "auth.json").write_text(
        json.dumps({"notFound": {"title": "We couldn't find that page"}}),
        encoding="utf-8",
    )
    (locale_root / "ko" / "auth.json").write_text(
        json.dumps(
            {"notFound": {"title": "해당 페이지를 찾을 수 없습니다"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (locale_root / "en" / "shell.json").write_text(json.dumps({}), encoding="utf-8")
    (locale_root / "ko" / "shell.json").write_text(json.dumps({}), encoding="utf-8")

    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "Editor.tsx").write_text(
        """
        import { useTranslation } from "react-i18next";
        export function Editor() {
          const { t } = useTranslation("shell");
          return (
            <h2>
              {t("notFound.title", {
                ns: "auth",
                defaultValue: "We couldn't find that page",
              })}
            </h2>
          );
        }
        """,
        encoding="utf-8",
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, src_root, allowlist)
    assert result.returncode == 0, result.stderr


def test_gate_ignores_a_nested_t_calls_ns_option_when_resolving_the_outer_call(
    tmp_path,
):
    """Found live while building this gate: AccountSecurityScreen.tsx has
    `t("account.workspaceMeta", { defaultValue: "...", visibility: t(`status.${x}`,
    { ns: "shell", ... }) })` — a nested `t()` call as an interpolation value,
    with its own `ns: "shell"` option. A substring search for `ns\\s*:` over
    the whole options text finds that nested option and wrongly attributes it
    to the *outer* call, which is actually bound to "auth" via the
    component's own `useTranslation("auth")` and exists there. Only a
    depth-aware parse of the outer object's *top-level* properties gets this
    right.
    """
    locale_root = tmp_path / "locales"
    (locale_root / "en").mkdir(parents=True)
    (locale_root / "ko").mkdir(parents=True)
    (locale_root / "en" / "auth.json").write_text(
        json.dumps({"account": {"workspaceMeta": "{{language}} · {{visibility}}"}}),
        encoding="utf-8",
    )
    (locale_root / "ko" / "auth.json").write_text(
        json.dumps(
            {"account": {"workspaceMeta": "{{language}} · {{visibility}}"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (locale_root / "en" / "shell.json").write_text(json.dumps({}), encoding="utf-8")
    (locale_root / "ko" / "shell.json").write_text(json.dumps({}), encoding="utf-8")

    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "AccountSecurityScreen.tsx").write_text(
        """
        import { useTranslation } from "react-i18next";
        export function AccountSecurityScreen() {
          const { t } = useTranslation("auth");
          return (
            <p>
              {t("account.workspaceMeta", {
                defaultValue: "{{language}} · {{visibility}}",
                language: workspace.default_language.toUpperCase(),
                visibility: t(`status.${workspace.visibility}`, {
                  ns: "shell",
                  defaultValue: workspace.visibility,
                }),
              })}
            </p>
          );
        }
        """,
        encoding="utf-8",
    )
    # workspaceMeta is allowlisted here only because the value is a pure
    # interpolation string that legitimately matches en byte-for-byte; the
    # point under test is namespace resolution, not translation coverage.
    allowlist = write_allowlist(tmp_path, ["auth.json:account.workspaceMeta"])
    result = run_gate(locale_root, src_root, allowlist)
    assert result.returncode == 0, result.stderr


def test_gate_reds_out_on_an_orphan_translate_helper_key(tmp_path):
    """The standalone `translate("ns:key", fallback)` helper (used outside
    React components, e.g. shared/lib/datetime.ts) has no `useTranslation`
    binding at all — its namespace comes only from the `ns:` prefix on the
    key itself. This exercises that colon-prefixed resolution path.
    """
    locale_root = write_locales(
        tmp_path,
        en={"time": {"unknownTime": "Unknown time"}},
        ko={"time": {"unknownTime": "시간 정보 없음"}},
        filename="test.json",
    )
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "datetime.ts").write_text(
        """
        import { translate } from "../i18n";
        export function formatDisplayDateTime(
          value,
          fallback = translate("test:time.unknownDate", "Unknown date"),
        ) {
          return fallback;
        }
        """,
        encoding="utf-8",
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, src_root, allowlist)
    assert result.returncode != 0
    assert "time.unknownDate" in result.stderr


def test_gate_reds_out_on_an_orphan_i18n_direct_call_key(tmp_path):
    """`i18n.t("ns:key", { defaultValue })` -- the shared i18next singleton
    called directly outside any `useTranslation` binding (e.g.
    shared/lib/activityLabels.ts, shared/api/client.ts, a validator inside
    SettingsScreen.tsx). The original callSiteRe excluded ANY call whose
    name was preceded by "." (needed so unrelated member calls, e.g.
    `foo.trim()`, are never mistaken for a translation call) -- which also
    made this real, live-in-production spelling structurally invisible: an
    orphan key reached this exact way rendered its English defaultValue
    forever with the gate reporting green the whole time.
    """
    locale_root = write_locales(
        tmp_path,
        en={"list": {"draftIndicator": "Unpublished draft"}},
        ko={"list": {"draftIndicator": "게시되지 않은 초안"}},
    )
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "activityLabels.ts").write_text(
        """
        import { i18n } from "../i18n";
        export function loadMoreLabel() {
          return i18n.t("test:list.loadMore", { defaultValue: "Load more" });
        }
        """,
        encoding="utf-8",
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, src_root, allowlist)
    assert result.returncode != 0
    assert "list.loadMore" in result.stderr
    assert "en/test.json" in result.stderr


def test_gate_does_not_flag_an_unrelated_dot_t_member_call(tmp_path):
    """Only a bare `i18n.t(` (the project's one, unaliased name for the
    shared singleton) is treated as a translation call. Neither some other
    object's `.t(` method (`foo.t(...)`, unrelated to i18next) nor "i18n" as
    someone else's nested property (`bar.i18n.t(...)`) should be flagged --
    the lookbehind must anchor the START of the match, not just exclude a
    literal ".t(".
    """
    locale_root = write_locales(tmp_path, en={"buckets": {}}, ko={"buckets": {}})
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "helper.ts").write_text(
        """
        export function formatGroupHeader(foo, bar) {
          const a = foo.t("buckets.unknownKey", { defaultValue: "Older" });
          const b = bar.i18n.t("buckets.otherUnknownKey", { defaultValue: "Newer" });
          return a + b;
        }
        """,
        encoding="utf-8",
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, src_root, allowlist)
    assert result.returncode == 0, result.stderr


def test_gate_reds_out_when_a_plural_fallback_masks_a_bare_key_with_no_count(tmp_path):
    """The plural-suffix fallback (added to stop I18N-05 from flagging ~20
    correctly-pluralized keys) must only apply when the call site actually
    passes a `count` option. Without that gate, a plain `t("list.loadMore",
    { defaultValue })` call -- no count, a genuine I18N-04 orphan -- passed
    silently whenever an UNRELATED key happened to share the same base and
    end in one of the six CLDR plural suffixes (here, "list.loadMore_other"
    means something else entirely; the base key was never translated).
    """
    locale_root = write_locales(
        tmp_path,
        en={"list": {"loadMore_other": "Load more (other)"}},
        ko={"list": {"loadMore_other": "더 보기(기타)"}},
    )
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "Screen.tsx").write_text(
        """
        import { useTranslation } from "react-i18next";
        export function Screen() {
          const { t } = useTranslation("test");
          return <span>{t("list.loadMore", { defaultValue: "Load more" })}</span>;
        }
        """,
        encoding="utf-8",
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, src_root, allowlist)
    assert result.returncode != 0
    assert "list.loadMore" in result.stderr


def test_gate_passes_a_genuine_pluralized_key_with_shorthand_count(tmp_path):
    """Positive control for the fix above: a REAL plural call -- `count`
    passed via ES6 shorthand (`{ count, defaultValue }`, the idiomatic form;
    this is exactly how frontend/src/features/setup/SetupScreen.tsx calls
    it) against a namespace that only has `_one`/`_other` variants and no
    bare key -- must still pass. `parseTopLevelObjectProps` previously
    dropped shorthand properties entirely (it only recognized "name: value",
    not a bare identifier), so `props.has("count")` was false even when the
    call plainly passed it, which would have made the gate above red out on
    every real pluralized call site using this idiom.
    """
    locale_root = write_locales(
        tmp_path,
        en={
            "list": {
                "itemCount_one": "{{count}} item",
                "itemCount_other": "{{count}} items",
            }
        },
        ko={"list": {"itemCount_one": "{{count}}개", "itemCount_other": "{{count}}개"}},
    )
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "Screen.tsx").write_text(
        """
        import { useTranslation } from "react-i18next";
        export function Screen({ count }) {
          const { t } = useTranslation("test");
          return <span>{t("list.itemCount", {
            count,
            defaultValue:
              count === 1 ? "{{count}} item" : "{{count}} items",
          })}</span>;
        }
        """,
        encoding="utf-8",
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, src_root, allowlist)
    assert result.returncode == 0, result.stderr


def test_gate_does_not_flag_a_t_function_received_as_a_parameter(tmp_path):
    """`formatGroupHeader(key, t)` — `t` here is a plain function parameter,
    not a `useTranslation` binding, so its namespace can't be resolved
    statically. The gate must skip it rather than guess, or every helper
    that accepts a translator callback would false-positive.
    """
    locale_root = write_locales(tmp_path, en={"buckets": {}}, ko={"buckets": {}})
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "helper.ts").write_text(
        """
        export function formatGroupHeader(key, t) {
          return t("buckets.unknownKey", { defaultValue: "Older" });
        }
        """,
        encoding="utf-8",
    )
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, src_root, allowlist)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scenario", ["missing_key", "extra_key"])
def test_gate_still_reds_out_on_the_original_key_set_mismatch(tmp_path, scenario):
    if scenario == "missing_key":
        en = {"a": "A", "b": "B"}
        ko = {"a": "가"}
    else:
        en = {"a": "A"}
        ko = {"a": "가", "b": "나"}
    locale_root = write_locales(tmp_path, en=en, ko=ko)
    allowlist = write_allowlist(tmp_path, [])
    result = run_gate(locale_root, empty_src(tmp_path), allowlist)
    assert result.returncode != 0
    assert "does not match" in result.stderr


def test_the_real_repository_locale_set_passes_the_gate():
    """Integration smoke against the actual production tree (no env
    overrides). This is the assertion that matters most in CI: it fails the
    moment a merge reintroduces a missing key, an untranslated value, or a
    dangling defaultValue key anywhere in the real app.
    """
    result = subprocess.run(
        ["node", str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
