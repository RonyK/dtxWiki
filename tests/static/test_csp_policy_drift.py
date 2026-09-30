from pathlib import Path

from backend.api.middleware.security_headers import SECURITY_HEADERS
from backend.scripts import sync_csp_templates
from backend.scripts.sync_csp_templates import TEMPLATE_PATHS, check, render


def test_production_proxy_csp_matches_application_policy() -> None:
    assert check() == ()


def test_new_proxy_template_without_csp_fails_closed(tmp_path: Path) -> None:
    for relative_path in TEMPLATE_PATHS:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'Content-Security-Policy "'
            + SECURITY_HEADERS["content-security-policy"]
            + '"\n',
            encoding="utf-8",
        )
    proxy_root = tmp_path / "infra" / "prod"
    (proxy_root / "Caddyfile.new.example").write_text(
        "wiki.example.test {\n\treverse_proxy localhost:8000\n}\n",
        encoding="utf-8",
    )

    assert check(root=tmp_path) == (
        "infra/prod/Caddyfile.new.example: expected exactly one "
        "Content-Security-Policy directive, found 0",
    )


def test_render_reports_all_invalid_templates_without_writing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    for relative_path in TEMPLATE_PATHS:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'Content-Security-Policy "outdated-policy"\n', encoding="utf-8"
        )
    proxy_root = tmp_path / "infra" / "prod"
    invalid_paths = (
        proxy_root / "Caddyfile.missing-one.example",
        proxy_root / "Caddyfile.missing-two.example",
    )
    for path in invalid_paths:
        path.write_text("reverse_proxy localhost:8000\n", encoding="utf-8")
    template_paths = tuple(tmp_path / relative_path for relative_path in TEMPLATE_PATHS)
    before = {path: path.read_bytes() for path in template_paths + invalid_paths}

    original_render = render
    monkeypatch.setattr(
        sync_csp_templates, "render", lambda: original_render(root=tmp_path)
    )

    assert sync_csp_templates.main([]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "infra/prod/Caddyfile.missing-one.example: expected exactly one "
        "Content-Security-Policy directive, found 0",
        "infra/prod/Caddyfile.missing-two.example: expected exactly one "
        "Content-Security-Policy directive, found 0",
    ]
    assert {path: path.read_bytes() for path in before} == before
