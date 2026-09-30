"""Synchronize production proxy templates with the application CSP."""

from __future__ import annotations

import argparse
from pathlib import Path

from backend.api.middleware.security_headers import SECURITY_HEADERS

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATHS = (
    Path("infra/prod/nginx.conf.example"),
    Path("infra/prod/Caddyfile.example"),
    Path("infra/prod/Caddyfile.internal.example"),
    Path("infra/prod/Caddyfile.insecure.example"),
    Path("infra/prod/Caddyfile.docker"),
    Path("infra/prod/Caddyfile.docker.internal"),
    Path("infra/prod/Caddyfile.custom.example"),
)
_HEADER_NAME = "Content-Security-Policy"


class TemplateRenderError(RuntimeError):
    """Raised when one or more proxy templates cannot be rendered."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = tuple(failures)
        super().__init__("\n".join(self.failures))


def render(*, root: Path = ROOT) -> tuple[Path, ...]:
    """Rewrite every tracked proxy template from the canonical app policy."""

    rendered_templates: list[tuple[Path, Path, str, str]] = []
    failures: list[str] = []
    for relative_path in _template_paths(root):
        path = root / relative_path
        try:
            current = _read_template(path)
            rendered = _render_content(current, relative_path=relative_path)
        except (OSError, ValueError) as exc:
            failures.append(_format_render_failure(relative_path, exc))
            continue
        rendered_templates.append((relative_path, path, current, rendered))

    if failures:
        raise TemplateRenderError(failures)

    changed: list[Path] = []
    for relative_path, path, current, rendered in rendered_templates:
        if rendered == current:
            continue
        path.write_bytes(rendered.encode("utf-8"))
        changed.append(relative_path)
    return tuple(changed)


def check(*, root: Path = ROOT) -> tuple[str, ...]:
    """Return deterministic drift diagnostics without modifying templates."""

    failures: list[str] = []
    for relative_path in _template_paths(root):
        path = root / relative_path
        try:
            current = _read_template(path)
            rendered = _render_content(current, relative_path=relative_path)
        except (OSError, ValueError) as exc:
            failures.append(_format_render_failure(relative_path, exc))
            continue
        if rendered != current:
            failures.append(
                f"{relative_path.as_posix()}: Content-Security-Policy drift"
            )
    return tuple(failures)


def _format_render_failure(relative_path: Path, exc: OSError | ValueError) -> str:
    return f"{relative_path.as_posix()}: {exc}"


def _read_template(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _template_paths(root: Path) -> tuple[Path, ...]:
    """Include known templates and discover newly added proxy templates.

    Name-based discovery intentionally includes a new Caddy/nginx template even
    when its author forgot the CSP header entirely.  ``check`` will then report
    the missing directive instead of silently accepting a policy-free proxy.
    """

    discovered: set[Path] = set(TEMPLATE_PATHS)
    production_root = root / "infra/prod"
    if production_root.is_dir():
        for candidate in production_root.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                contains_header = _HEADER_NAME.encode() in candidate.read_bytes()
            except OSError:
                contains_header = False
            name = candidate.name.casefold()
            is_proxy_template = name.startswith("caddyfile") or (
                name.startswith("nginx") and ".conf" in name
            )
            if contains_header or is_proxy_template:
                discovered.add(candidate.relative_to(root))
    return tuple(sorted(discovered, key=lambda path: path.as_posix()))


def _render_content(content: str, *, relative_path: Path) -> str:
    matching_indexes = [
        index
        for index, line in enumerate(content.splitlines(keepends=True))
        if _HEADER_NAME in line
    ]
    if len(matching_indexes) != 1:
        raise ValueError(
            "expected exactly one Content-Security-Policy directive, "
            f"found {len(matching_indexes)}"
        )

    lines = content.splitlines(keepends=True)
    index = matching_indexes[0]
    line = lines[index]
    opening_quote = line.find('"')
    closing_quote = line.find('"', opening_quote + 1)
    if opening_quote < 0 or closing_quote < 0:
        raise ValueError(
            f"{relative_path.as_posix()} CSP directive must use one quoted value"
        )
    lines[index] = (
        line[: opening_quote + 1]
        + SECURITY_HEADERS["content-security-policy"]
        + line[closing_quote:]
    )
    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift without rewriting templates",
    )
    args = parser.parse_args(argv)
    if args.check:
        failures = check()
        for failure in failures:
            print(failure)
        return 1 if failures else 0
    try:
        changed_paths = render()
    except TemplateRenderError as exc:
        for failure in exc.failures:
            print(failure)
        return 1
    for changed_path in changed_paths:
        print(changed_path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
