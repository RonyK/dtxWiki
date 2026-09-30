import ast
import importlib.util
from pathlib import Path

import pytest

from tests.static._python_ast import ROOT, parse_python_file

INNER_LAYER_CONCRETE_IMPORTS = (
    "backend.api",
    "backend.app",
    "backend.converters",
    "backend.mcp",
    "backend.migrations",
    "backend.models",
    "backend.policy",
    "backend.repositories",
    "backend.schemas",
    "backend.services",
    "backend.spi",
    "backend.utils",
    "backend.worker",
    "fastapi",
    "os",
    "pathlib",
    "shutil",
    "sqlite3",
    "sqlalchemy",
    "starlette",
    "subprocess",
    "tempfile",
)

FORBIDDEN_IMPORTS = {
    "backend": ("dtxwiki_cloud",),
    "backend/api": ("backend.models", "backend.repositories"),
    "backend/mcp": ("backend.models", "backend.repositories"),
    "backend/repositories": ("backend.api", "backend.mcp", "backend.services"),
    "backend/schemas": ("backend.api", "backend.app", "backend.mcp"),
    "backend/services": ("backend.api", "backend.mcp"),
    "backend/utils": ("backend.api", "backend.app", "backend.mcp", "backend.schemas"),
    "backend/media": (
        "backend.api",
        "backend.context_media",
        "backend.converters",
        "backend.library_media",
        "backend.mcp",
        "backend.models.context",
        "backend.models.library",
        "backend.models.library_discovery",
        "backend.models.upload",
        "backend.repositories",
        "backend.services",
        "backend.spi",
    ),
    "backend/media/domain": (
        "backend.media.application",
        "backend.media.adapters",
        *INNER_LAYER_CONCRETE_IMPORTS,
    ),
    "backend/media/ports": (
        "backend.media.application",
        "backend.media.adapters",
        *INNER_LAYER_CONCRETE_IMPORTS,
    ),
    "backend/media/application": (
        "backend.media.adapters",
        *INNER_LAYER_CONCRETE_IMPORTS,
    ),
    "backend/media/adapters": ("backend.app",),
    "backend/library_media": ("backend.context_media",),
    "backend/library_media/domain": (
        "backend.library_media.application",
        "backend.library_media.adapters",
        "backend.media.application",
        "backend.media.adapters",
        *INNER_LAYER_CONCRETE_IMPORTS,
    ),
    "backend/library_media/ports": (
        "backend.library_media.application",
        "backend.library_media.adapters",
        "backend.media.application",
        "backend.media.adapters",
        *INNER_LAYER_CONCRETE_IMPORTS,
    ),
    "backend/library_media/application": (
        "backend.library_media.adapters",
        "backend.media.application",
        "backend.media.adapters",
        *INNER_LAYER_CONCRETE_IMPORTS,
    ),
    "backend/library_media/adapters": ("backend.app",),
    "backend/context_media": (
        "backend.converters",
        "backend.library_media",
        "backend.spi",
    ),
    "backend/context_media/domain": (
        "backend.context_media.application",
        "backend.context_media.adapters",
        "backend.media.application",
        "backend.media.adapters",
        *INNER_LAYER_CONCRETE_IMPORTS,
    ),
    "backend/context_media/ports": (
        "backend.context_media.application",
        "backend.context_media.adapters",
        "backend.media.application",
        "backend.media.adapters",
        *INNER_LAYER_CONCRETE_IMPORTS,
    ),
    "backend/context_media/application": (
        "backend.context_media.adapters",
        "backend.media.application",
        "backend.media.adapters",
        *INNER_LAYER_CONCRETE_IMPORTS,
    ),
    "backend/context_media/adapters": ("backend.app",),
}

CONCRETE_MEDIA_IMPORTS = (
    "backend.media.adapters",
    "backend.library_media.adapters",
    "backend.context_media.adapters",
)
ADAPTER_PACKAGE_PATHS = (
    ("backend/media/adapters", "backend.media.adapters"),
    ("backend/library_media/adapters", "backend.library_media.adapters"),
    ("backend/context_media/adapters", "backend.context_media.adapters"),
)
MEDIA_PACKAGE_LAYERS = {
    "backend/media": frozenset({"domain", "ports", "application", "adapters"}),
    "backend/library_media": frozenset(
        {"domain", "ports", "application", "adapters"}
    ),
    "backend/context_media": frozenset(
        {"domain", "ports", "application", "adapters"}
    ),
}
REQUIRED_MEDIA_INNER_LAYERS = ("domain", "ports", "application")
MEDIA_COMPOSITION_ROOT = "backend/app/container.py"


def iter_python_files(package_path: str, *, root: Path = ROOT):
    directory = root / package_path
    if not directory.exists():
        return
    yield from directory.rglob("*.py")


def imports_from(path: Path, *, root: Path = ROOT) -> set[str]:
    tree = parse_python_file(path, root=root)
    imports: set[str] = set()
    importlib_modules: set[str] = set()
    import_module_functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
            importlib_modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "importlib"
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = list(path.relative_to(root).parent.parts)
                parent_count = node.level - 1
                if parent_count > len(package_parts):
                    continue
                module_parts = package_parts[: len(package_parts) - parent_count]
                if node.module:
                    module_parts.extend(node.module.split("."))
                module = ".".join(module_parts)
            else:
                module = node.module or ""

            if module:
                imports.add(module)
                imports.update(f"{module}.{alias.name}" for alias in node.names)
            if node.level == 0 and node.module == "importlib":
                import_module_functions.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )
        elif isinstance(node, ast.Call):
            target = node.func
            is_builtin_import = (
                isinstance(target, ast.Name) and target.id == "__import__"
            )
            is_import_module = (
                isinstance(target, ast.Name)
                and target.id in import_module_functions
            ) or (
                isinstance(target, ast.Attribute)
                and target.attr == "import_module"
                and isinstance(target.value, ast.Name)
                and target.value.id in importlib_modules
            )
            if not (is_builtin_import or is_import_module):
                continue
            name_argument = node.args[0] if node.args else next(
                (keyword.value for keyword in node.keywords if keyword.arg == "name"),
                None,
            )
            if not (
                isinstance(name_argument, ast.Constant)
                and isinstance(name_argument.value, str)
            ):
                continue
            imported_name = name_argument.value
            if is_import_module and imported_name.startswith("."):
                package_argument = node.args[1] if len(node.args) > 1 else next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg == "package"
                    ),
                    None,
                )
                if not (
                    isinstance(package_argument, ast.Constant)
                    and isinstance(package_argument.value, str)
                ):
                    continue
                try:
                    imported_name = importlib.util.resolve_name(
                        imported_name,
                        package_argument.value,
                    )
                except ImportError:
                    continue
            imports.add(imported_name)
    return imports


def has_prefix(imported: str, prefix: str) -> bool:
    return imported == prefix or imported.startswith(f"{prefix}.")


def shortest_matching_imports(imports: set[str], prefix: str) -> set[str]:
    matches = {imported for imported in imports if has_prefix(imported, prefix)}
    return {
        imported
        for imported in matches
        if not any(
            imported != other and imported.startswith(f"{other}.")
            for other in matches
        )
    }


def most_specific_matching_imports(imports: set[str], prefix: str) -> set[str]:
    matches = {imported for imported in imports if has_prefix(imported, prefix)}
    return {
        imported
        for imported in matches
        if not any(
            imported != other and other.startswith(f"{imported}.")
            for other in matches
        )
    }


def directional_violations(*, root: Path = ROOT) -> list[str]:
    violations: list[str] = []
    for package_path, forbidden_prefixes in FORBIDDEN_IMPORTS.items():
        for source_file in iter_python_files(package_path, root=root):
            imported_modules = imports_from(source_file, root=root)
            for forbidden in forbidden_prefixes:
                for imported in shortest_matching_imports(imported_modules, forbidden):
                    relative = source_file.relative_to(root).as_posix()
                    violations.append(f"{relative} imports {imported}")
    return sorted(set(violations))


def concrete_adapter_violations(*, root: Path = ROOT) -> list[str]:
    """Scan every backend importer, not merely future media directories."""
    violations: list[str] = []
    for source_file in iter_python_files("backend", root=root):
        relative = source_file.relative_to(root).as_posix()
        # Integration tests must be able to instantiate an adapter family in
        # isolation.  The composition-root rule constrains shipped runtime
        # modules, not test harnesses that never participate in application
        # dependency injection.
        if relative.startswith("backend/tests/"):
            continue
        if relative == MEDIA_COMPOSITION_ROOT:
            continue
        source_adapter = next(
            (
                (package_path, import_prefix)
                for package_path, import_prefix in ADAPTER_PACKAGE_PATHS
                if relative == f"{package_path}.py"
                or relative.startswith(f"{package_path}/")
            ),
            None,
        )
        imported_modules = imports_from(source_file, root=root)
        for prefix in CONCRETE_MEDIA_IMPORTS:
            for imported in most_specific_matching_imports(imported_modules, prefix):
                normalized_import = imported
                if imported != prefix:
                    import_suffix = imported.removeprefix(f"{prefix}.")
                    import_family = import_suffix.split(".", 1)[0]
                    normalized_import = f"{prefix}.{import_family}"
                else:
                    import_family = None
                if source_adapter is not None and prefix == source_adapter[1]:
                    package_path, _import_prefix = source_adapter
                    source_suffix = relative.removeprefix(f"{package_path}/")
                    source_family = source_suffix.split("/", 1)[0].removesuffix(".py")
                    if (
                        source_family != "__init__"
                        and source_family == import_family
                    ):
                        continue
                violations.append(
                    f"{relative} imports concrete adapter {normalized_import}"
                )
    return sorted(set(violations))


def media_package_shape_violations(*, root: Path = ROOT) -> list[str]:
    violations: list[str] = []
    for package_path, allowed_layers in MEDIA_PACKAGE_LAYERS.items():
        package = root / package_path
        if not package.exists():
            continue
        for child in package.iterdir():
            if child.name == "__pycache__":
                continue
            relative = child.relative_to(root).as_posix()
            if child.is_file() and child.name != "__init__.py":
                violations.append(f"{relative} is an unexpected package-root module")
            elif child.is_dir() and child.name not in allowed_layers:
                violations.append(f"{relative} is an unexpected package layer")
    return sorted(violations)


def existing_media_package_completeness_violations(
    *, root: Path = ROOT
) -> list[str]:
    """Activate G1b completeness per package once that I2/I4/I5 package exists."""

    violations: list[str] = []
    for package_path in MEDIA_PACKAGE_LAYERS:
        package = root / package_path
        if not package.exists():
            continue
        for layer in REQUIRED_MEDIA_INNER_LAYERS:
            directory = package / layer
            has_source = directory.is_dir() and any(
                candidate.is_file()
                and candidate.suffix == ".py"
                and candidate.name != "__init__.py"
                for candidate in directory.rglob("*.py")
            )
            if not has_source:
                violations.append(
                    f"{package_path}/{layer} has no implementation source"
                )
    return sorted(violations)


def test_g1a_prospective_backend_import_boundaries_are_directional():
    assert directional_violations() == []


def test_g1a_prospective_media_adapters_are_composed_only_in_container():
    assert concrete_adapter_violations() == []


def test_g1a_prospective_media_package_shape_is_bounded():
    assert media_package_shape_violations() == []


def test_g1b_existing_media_packages_have_nonempty_inner_layers():
    assert existing_media_package_completeness_violations() == []


def test_repository_ast_cache_is_shared_across_static_gate_consumers():
    from tests.static import test_mcp_app_ocp_contract as mcp_app_contract

    source = ROOT / "backend/app/main.py"

    assert mcp_app_contract.parse_python_file is parse_python_file
    assert mcp_app_contract.parse_python_file(source) is parse_python_file(source)


def test_external_ast_path_is_not_cached_when_root_is_omitted(tmp_path: Path):
    source = tmp_path / "rewritten.py"
    source.write_text("BEFORE = 1\n", encoding="utf-8")
    before = parse_python_file(source)

    source.write_text("AFTER = 2\n", encoding="utf-8")
    after = parse_python_file(source)

    assert before is not after
    assert before.body[0].targets[0].id == "BEFORE"
    assert after.body[0].targets[0].id == "AFTER"


def write_fixture(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize(
    ("relative", "source", "expected"),
    [
        (
            "backend/media/domain/blob.py",
            "from sqlalchemy.orm import Session\n",
            "backend/media/domain/blob.py imports sqlalchemy.orm",
        ),
        (
            "backend/library_media/application/convert.py",
            "import backend.media.adapters.filesystem\n",
            "backend/library_media/application/convert.py imports "
            "backend.media.adapters.filesystem",
        ),
        (
            "backend/context_media/application/save.py",
            "import backend.spi.conversion\n",
            "backend/context_media/application/save.py imports backend.spi.conversion",
        ),
        (
            "backend/media/domain/blob.py",
            "from .. import adapters\n",
            "backend/media/domain/blob.py imports backend.media.adapters",
        ),
        (
            "backend/media/ports/blob_store.py",
            "from backend.repositories.media import MediaRepository\n",
            "backend/media/ports/blob_store.py imports backend.repositories.media",
        ),
        (
            "backend/media/application/install.py",
            "from backend.app.container import container\n",
            "backend/media/application/install.py imports backend.app.container",
        ),
        (
            "backend/library_media/domain/version.py",
            "from backend.services.backup_service import BackupService\n",
            "backend/library_media/domain/version.py imports "
            "backend.services.backup_service",
        ),
        (
            "backend/library_media/application/convert.py",
            "from backend.context_media.domain.asset import Asset\n",
            "backend/library_media/application/convert.py imports "
            "backend.context_media.domain.asset",
        ),
        (
            "backend/library_media/application/convert.py",
            "from backend.media.application.install import InstallBlob\n",
            "backend/library_media/application/convert.py imports "
            "backend.media.application.install",
        ),
        (
            "backend/context_media/ports/assets.py",
            "from sqlalchemy.orm import Session\n",
            "backend/context_media/ports/assets.py imports sqlalchemy.orm",
        ),
        (
            "backend/context_media/application/upload.py",
            "from backend.spi import ConverterPlugin\n",
            "backend/context_media/application/upload.py imports backend.spi",
        ),
        (
            "backend/context_media/application/upload.py",
            "import backend.converters.registry\n",
            "backend/context_media/application/upload.py imports "
            "backend.converters.registry",
        ),
        (
            "backend/media/application/install.py",
            "import os\n",
            "backend/media/application/install.py imports os",
        ),
        (
            "backend/media/application/install.py",
            "import importlib\n"
            "importlib.import_module('backend.repositories.media')\n",
            "backend/media/application/install.py imports backend.repositories.media",
        ),
        (
            "backend/context_media/application/upload.py",
            "__import__('backend.converters.pdf')\n",
            "backend/context_media/application/upload.py imports "
            "backend.converters.pdf",
        ),
        (
            "backend/media/adapters/repository.py",
            "from backend.models.library import LibraryBlob\n",
            "backend/media/adapters/repository.py imports backend.models.library",
        ),
        (
            "backend/media/adapters/conversion.py",
            "from backend.spi import ConverterPlugin\n",
            "backend/media/adapters/conversion.py imports backend.spi",
        ),
    ],
)
def test_directional_gate_detects_deterministic_fixture_violations(
    tmp_path: Path, relative: str, source: str, expected: str
):
    write_fixture(tmp_path, relative, source)
    assert expected in directional_violations(root=tmp_path)


def test_directional_gate_accepts_inward_port_fixture(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/library_media/application/convert.py",
        "from backend.media.ports.blob_store import BlobStore\n",
    )
    assert directional_violations(root=tmp_path) == []


def test_directional_gate_accepts_relative_inward_port_fixture(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/library_media/application/convert.py",
        "from ...media.ports import blob_store\n",
    )
    assert directional_violations(root=tmp_path) == []


def test_directional_gate_accepts_application_inward_dependencies(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/library_media/application/convert.py",
        "\n".join(
            (
                "from backend.library_media.domain.item import LibraryItem",
                "from backend.library_media.ports.conversion import Converter",
                "from backend.media.domain.blob import StoredBlob",
                "from backend.media.ports.blob_store import BlobStore",
            )
        ),
    )
    assert directional_violations(root=tmp_path) == []


def test_directional_gate_accepts_adapter_inward_and_framework_dependencies(
    tmp_path: Path,
):
    write_fixture(
        tmp_path,
        "backend/context_media/adapters/repository.py",
        "\n".join(
            (
                "from backend.context_media.application.upload import UploadAsset",
                "from backend.context_media.domain.asset import ContextMediaAsset",
                "from backend.context_media.ports.assets import AssetRepository",
                "from backend.media.ports.blob_store import BlobStore",
                "from sqlalchemy.orm import Session",
            )
        ),
    )
    assert directional_violations(root=tmp_path) == []


def test_adapter_gate_scans_all_backend_importers_and_detects_violation(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/accidental_composition.py",
        "from backend.media.adapters.filesystem import FileBlobStore\n",
    )
    assert concrete_adapter_violations(root=tmp_path) == [
        "backend/services/accidental_composition.py imports concrete adapter "
        "backend.media.adapters.filesystem"
    ]


def test_adapter_gate_detects_relative_import_from_any_backend_package(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/accidental_composition.py",
        "from ..media import adapters\n",
    )
    assert concrete_adapter_violations(root=tmp_path) == [
        "backend/services/accidental_composition.py imports concrete adapter "
        "backend.media.adapters"
    ]


def test_adapter_gate_allows_import_within_same_adapter_package(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/media/adapters/formats/images.py",
        "from .base import FormatValidator\n",
    )
    assert concrete_adapter_violations(root=tmp_path) == []


@pytest.mark.parametrize(
    "source",
    [
        "from backend.media.adapters import formats\n",
        "from .. import formats\n",
    ],
)
def test_adapter_gate_allows_parent_import_of_same_adapter_family(
    tmp_path: Path, source: str
):
    write_fixture(
        tmp_path,
        "backend/media/adapters/formats/images.py",
        source,
    )
    assert concrete_adapter_violations(root=tmp_path) == []


def test_adapter_gate_rejects_cross_family_import_in_same_adapter_package(
    tmp_path: Path,
):
    write_fixture(
        tmp_path,
        "backend/media/adapters/bootstrap.py",
        "from backend.media.adapters.filesystem import FileBlobStore\n"
        "from backend.media.adapters.repository import BlobRepository\n",
    )
    assert concrete_adapter_violations(root=tmp_path) == [
        "backend/media/adapters/bootstrap.py imports concrete adapter "
        "backend.media.adapters.filesystem",
        "backend/media/adapters/bootstrap.py imports concrete adapter "
        "backend.media.adapters.repository",
    ]


def test_directional_gate_rejects_adapter_import_of_container(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/media/adapters/filesystem.py",
        "from backend.app.container import container\n",
    )
    assert directional_violations(root=tmp_path) == [
        "backend/media/adapters/filesystem.py imports backend.app.container"
    ]


def test_adapter_gate_detects_constant_dynamic_import(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/accidental_composition.py",
        "from importlib import import_module as load\n"
        "load('backend.context_media.adapters.repository')\n",
    )
    assert concrete_adapter_violations(root=tmp_path) == [
        "backend/services/accidental_composition.py imports concrete adapter "
        "backend.context_media.adapters.repository"
    ]


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\n"
        "importlib.import_module(name='backend.media.adapters.filesystem')\n",
        "import importlib\n"
        "importlib.import_module("
        "'.filesystem', package='backend.media.adapters')\n",
    ],
)
def test_adapter_gate_detects_keyword_and_relative_dynamic_imports(
    tmp_path: Path, source: str
):
    write_fixture(
        tmp_path,
        "backend/services/accidental_composition.py",
        source,
    )
    assert concrete_adapter_violations(root=tmp_path) == [
        "backend/services/accidental_composition.py imports concrete adapter "
        "backend.media.adapters.filesystem"
    ]


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (
            "backend/media/bootstrap.py",
            "backend/media/bootstrap.py is an unexpected package-root module",
        ),
        (
            "backend/context_media/infrastructure/store.py",
            "backend/context_media/infrastructure is an unexpected package layer",
        ),
    ],
)
def test_media_package_shape_gate_rejects_layer_bypasses(
    tmp_path: Path, relative: str, expected: str
):
    write_fixture(tmp_path, relative, "pass\n")
    assert expected in media_package_shape_violations(root=tmp_path)


def test_existing_media_package_completeness_activates_per_package(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/media/domain/blob.py",
        "class StoredBlob:\n    pass\n",
    )
    assert existing_media_package_completeness_violations(root=tmp_path) == [
        "backend/media/application has no implementation source",
        "backend/media/ports has no implementation source",
    ]


def test_adapter_gate_allows_only_container_fixture(tmp_path: Path):
    write_fixture(
        tmp_path,
        MEDIA_COMPOSITION_ROOT,
        "from backend.media.adapters.filesystem import FileBlobStore\n",
    )
    assert concrete_adapter_violations(root=tmp_path) == []
