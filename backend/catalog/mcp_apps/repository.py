"""Validated repository for the package-owned MCP application catalog."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlparse

from pydantic import TypeAdapter, ValidationError

from backend.catalog.mcp_apps.models import (
    AccessTokenAtom,
    AppCommandArtifact,
    CommandArtifact,
    DeepLinkArtifact,
    ExecutableDefinition,
    JsonConfigArtifact,
    LiteralAtom,
    McpAppCatalogV1,
    McpAppDefinition,
    McpConnectionVariant,
    McpGenericProfile,
    McpSetupRecipe,
    McpUrlAtom,
    SecretTemplateAtom,
)
from backend.catalog.mcp_apps.schema import catalog_json_schema
from backend.catalog.mcp_apps.security import (
    has_unsafe_control_characters,
    is_approved_command_argv_secret_sink,
    is_approved_command_env_secret_sink,
    is_approved_json_access_token_sink,
    is_safe_command_env_name,
    is_slash_command_shaped,
)

CATALOG_RESOURCE = "catalog.v1.json"
CATALOG_SCHEMA_RESOURCE = "catalog.schema.json"
_REVISION_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
_FORBIDDEN_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "sh",
    "zsh",
}
_FORBIDDEN_COMMAND_STRING_ARGUMENTS = {"-c", "/c", "-command", "--command"}
_PLATFORMS = ("windows", "macos", "linux")
_SHELLS = ("powershell", "bash", "zsh")
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_SVG_ALLOWED_ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "title",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
    }
)
_SVG_ALLOWED_ATTRIBUTES = frozenset(
    {
        "viewBox",
        "role",
        "aria-label",
        "width",
        "height",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "fill",
        "fill-rule",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",
        "opacity",
        "transform",
    }
)
_SVG_FORBIDDEN_VALUE_MARKERS = ("javascript:", "data:", "url(", "<", ">")
_SECRET_ATOM_ADAPTER = TypeAdapter(SecretTemplateAtom)


class CatalogValidationError(RuntimeError):
    """Raised when a packaged catalog violates the closed vocabulary."""


@dataclass(frozen=True)
class VerifiedOAuthAssociation:
    catalog_app_id: str
    label_snapshot: str
    catalog_revision: str
    metadata_fingerprint: str
    provenance_ref: str


class CatalogResourceRoot(Protocol):
    def joinpath(self, *descendants: str): ...


class CatalogRepository:
    """Read-only port over one completely validated catalog snapshot."""

    def __init__(self, resource_root: CatalogResourceRoot | Path | None = None) -> None:
        self._resource_root = resource_root or resources.files(
            "backend.catalog.mcp_apps"
        )
        self._snapshot: McpAppCatalogV1 | None = None

    def snapshot(self) -> McpAppCatalogV1:
        if self._snapshot is None:
            self._snapshot = self._load_and_validate()
        return self._snapshot

    def find_app(self, app_id: str) -> McpAppDefinition | None:
        normalized = app_id.strip().casefold()
        return next(
            (app for app in self.snapshot().apps if app.id == normalized),
            None,
        )

    def find_generic_profile(self, profile_id: str) -> McpGenericProfile | None:
        normalized = profile_id.strip().casefold()
        return next(
            (
                profile
                for profile in self.snapshot().generic_profiles
                if profile.id == normalized
            ),
            None,
        )

    def find_variant(self, variant_id: str) -> McpConnectionVariant | None:
        normalized = variant_id.strip().casefold()
        for owner in (*self.snapshot().apps, *self.snapshot().generic_profiles):
            for variant in owner.variants:
                if variant.id == normalized:
                    return variant
        return None

    def find_recipe(self, recipe_id: str) -> McpSetupRecipe | None:
        normalized = recipe_id.strip().casefold()
        return next(
            (recipe for recipe in self.snapshot().recipes if recipe.id == normalized),
            None,
        )

    def find_executable(self, executable_id: str) -> ExecutableDefinition | None:
        normalized = executable_id.strip().casefold()
        return next(
            (
                executable
                for executable in self.snapshot().executables
                if executable.id == normalized
            ),
            None,
        )

    def label_for_app(self, app_id: str, *, locale: str = "en") -> str | None:
        app = self.find_app(app_id)
        if app is None:
            return None
        return app.labels.ko if locale == "ko" else app.labels.en

    def active_app_ids(self) -> frozenset[str]:
        return frozenset(
            app.id for app in self.snapshot().apps if app.lifecycle == "active"
        )

    def find_verified_oauth_association(
        self,
        metadata_fingerprint: str,
    ) -> VerifiedOAuthAssociation | None:
        """Resolve only an exact, reviewed registration metadata fingerprint."""

        normalized = metadata_fingerprint.strip().casefold()
        for app in self.snapshot().apps:
            if app.lifecycle != "active":
                continue
            for identity in app.oauth_identities or ():
                if identity.normalized_metadata_fingerprint == normalized:
                    return VerifiedOAuthAssociation(
                        catalog_app_id=app.id,
                        label_snapshot=app.labels.en,
                        catalog_revision=self.snapshot().catalog_revision,
                        metadata_fingerprint=normalized,
                        provenance_ref=identity.provenance_ref,
                    )
        return None

    def icon_bytes(self, app_id: str, *, generic: bool = False) -> tuple[bytes, str]:
        owner = (
            self.find_generic_profile(app_id) if generic else self.find_app(app_id)
        )
        if owner is None:
            raise KeyError(app_id)
        return self._read_bytes(owner.icon.asset_path), owner.icon.media_type

    def _load_and_validate(self) -> McpAppCatalogV1:
        try:
            payload = json.loads(
                self._read_text(CATALOG_RESOURCE),
                parse_constant=_reject_non_json_constant,
            )
            catalog = McpAppCatalogV1.model_validate(payload)
        except (OSError, ValueError, ValidationError) as exc:
            raise CatalogValidationError(f"invalid MCP app catalog: {exc}") from exc
        self._validate_catalog(catalog)
        # The schema is a shipped public artifact. Parsing it on every cold
        # load catches packaging/truncation errors even though Pydantic owns
        # runtime validation and the repository owns cross-reference checks.
        try:
            schema = json.loads(
                self._read_text(CATALOG_SCHEMA_RESOURCE),
                parse_constant=_reject_non_json_constant,
            )
        except (OSError, ValueError) as exc:
            raise CatalogValidationError(f"invalid catalog JSON schema: {exc}") from exc
        if schema != catalog_json_schema():
            raise CatalogValidationError(
                "catalog JSON schema is stale; regenerate it from McpAppCatalogV1"
            )
        return catalog

    def _validate_catalog(self, catalog: McpAppCatalogV1) -> None:
        if not _REVISION_PATTERN.fullmatch(catalog.catalog_revision):
            raise CatalogValidationError(
                "catalogRevision must be a dated immutable revision"
            )
        _require_unique("app id", [app.id for app in catalog.apps])
        _require_unique(
            "generic profile id",
            [profile.id for profile in catalog.generic_profiles],
        )
        _require_unique(
            "executable id", [executable.id for executable in catalog.executables]
        )
        _require_unique("recipe id", [recipe.id for recipe in catalog.recipes])

        owners = (*catalog.apps, *catalog.generic_profiles)
        variants = [variant for owner in owners for variant in owner.variants]
        _require_unique("variant id", [variant.id for variant in variants])
        variant_by_id = {variant.id: variant for variant in variants}
        recipe_by_id = {recipe.id: recipe for recipe in catalog.recipes}
        executable_by_id = {
            executable.id: executable for executable in catalog.executables
        }

        featured = [
            app for app in catalog.apps if app.featured_rank is not None
        ]
        if len(featured) > 4:
            raise CatalogValidationError("at most four apps may be featured")
        _require_unique(
            "featured rank",
            [str(app.featured_rank) for app in featured],
        )
        self._validate_search_terms(catalog)
        oauth_fingerprints: set[str] = set()
        for app in catalog.apps:
            provenance_urls = {
                item.source_url for item in app.verification.provenance
            }
            for identity in app.oauth_identities or ():
                fingerprint = identity.normalized_metadata_fingerprint
                if fingerprint in oauth_fingerprints:
                    raise CatalogValidationError(
                        f"duplicate OAuth metadata fingerprint: {fingerprint}"
                    )
                oauth_fingerprints.add(fingerprint)
                if identity.provenance_ref not in provenance_urls:
                    raise CatalogValidationError(
                        f"OAuth identity for {app.id} has unknown provenance"
                    )

        for executable in catalog.executables:
            if executable.command_name.casefold() in _FORBIDDEN_EXECUTABLES:
                raise CatalogValidationError(
                    f"shell interpreter is not an approved executable: {executable.id}"
                )

        for owner in owners:
            self._validate_icon(
                owner.icon.asset_path,
                owner.icon.sha256,
                owner.icon.media_type,
            )
            for variant in owner.variants:
                if len(set(variant.endpoint_reachability)) != len(
                    variant.endpoint_reachability
                ):
                    raise CatalogValidationError(
                        f"duplicate reachability in variant {variant.id}"
                    )
                if len(set(variant.recipe_ids)) != len(variant.recipe_ids):
                    raise CatalogValidationError(
                        f"duplicate recipe reference in variant {variant.id}"
                    )
                for recipe_id in variant.recipe_ids:
                    recipe = recipe_by_id.get(recipe_id)
                    if recipe is None:
                        raise CatalogValidationError(
                            f"variant {variant.id} references missing recipe "
                            f"{recipe_id}"
                        )
                    if recipe.variant_id != variant.id:
                        raise CatalogValidationError(
                            f"recipe {recipe.id} belongs to {recipe.variant_id}, "
                            f"not {variant.id}"
                        )
                self._validate_recipe_coverage(
                    variant,
                    tuple(recipe_by_id[item] for item in variant.recipe_ids),
                )

        selector_keys: set[tuple[str, str | None, str | None, str | None]] = set()
        for recipe in catalog.recipes:
            variant = variant_by_id.get(recipe.variant_id)
            if variant is None:
                raise CatalogValidationError(
                    f"recipe {recipe.id} references missing variant {recipe.variant_id}"
                )
            selector_key = (
                recipe.variant_id,
                recipe.selectors.platform,
                recipe.selectors.shell,
                recipe.selectors.app_version,
            )
            if selector_key in selector_keys:
                raise CatalogValidationError(
                    f"ambiguous recipe selectors for variant {recipe.variant_id}"
                )
            selector_keys.add(selector_key)
            self._validate_recipe(
                recipe,
                variant=variant,
                executable_ids=frozenset(executable_by_id),
            )

        for app in catalog.apps:
            if app.verification.tier == "verified":
                if not app.verification.verified_at or not app.verification.provenance:
                    raise CatalogValidationError(
                        f"verified app {app.id} needs date and provenance"
                    )
            for provenance in app.verification.provenance:
                parsed = urlparse(provenance.source_url)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise CatalogValidationError(
                        f"app {app.id} provenance must be public HTTPS"
                    )

    def _validate_search_terms(self, catalog: McpAppCatalogV1) -> None:
        owners: dict[str, str] = {}
        for app in catalog.apps:
            identity_terms = (app.id, *app.aliases)
            normalized_identity_terms = [
                " ".join(raw_term.casefold().split())
                for raw_term in identity_terms
            ]
            if len(normalized_identity_terms) != len(set(normalized_identity_terms)):
                raise CatalogValidationError(
                    f"duplicate id or alias in app {app.id}"
                )
            # English and Korean labels are allowed to be identical product
            # names. They collapse to one search term rather than being
            # treated as an alias collision.
            terms = dict.fromkeys(
                (*identity_terms, app.labels.en, app.labels.ko)
            )
            for raw_term in terms:
                term = " ".join(raw_term.casefold().split())
                if not term:
                    raise CatalogValidationError(f"blank search alias in app {app.id}")
                previous = owners.setdefault(term, app.id)
                if previous != app.id:
                    raise CatalogValidationError(
                        f"ambiguous app alias {raw_term}: {previous}, {app.id}"
                    )

    def _validate_icon(
        self,
        asset_path: str,
        expected_sha256: str,
        media_type: str,
    ) -> None:
        path = PurePosixPath(asset_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) != 2
            or path.parts[0] != "icons"
            or path.suffix not in {".svg", ".png"}
        ):
            raise CatalogValidationError(f"unsafe catalog icon path: {asset_path}")
        content = self._read_bytes(asset_path)
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected_sha256:
            raise CatalogValidationError(f"catalog icon hash mismatch: {asset_path}")
        if path.suffix == ".svg":
            if media_type != "image/svg+xml":
                raise CatalogValidationError(
                    f"catalog icon media mismatch: {asset_path}"
                )
            self._validate_svg(content, asset_path=asset_path)
        elif media_type != "image/png" or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise CatalogValidationError(f"invalid PNG catalog icon: {asset_path}")

    def _validate_svg(self, content: bytes, *, asset_path: str) -> None:
        if len(content) > 64 * 1024 or b"<!" in content:
            raise CatalogValidationError(f"unsafe SVG content: {asset_path}")
        without_declaration = content
        if without_declaration.lstrip().startswith(b"<?xml"):
            declaration_end = without_declaration.find(b"?>")
            if declaration_end < 0:
                raise CatalogValidationError(f"unsafe SVG content: {asset_path}")
            without_declaration = without_declaration[declaration_end + 2 :]
        if b"<?" in without_declaration:
            raise CatalogValidationError(f"unsafe SVG content: {asset_path}")
        try:
            root = ET.fromstring(content)
        except (ET.ParseError, ValueError) as exc:
            raise CatalogValidationError(f"invalid SVG content: {asset_path}") from exc
        elements = list(root.iter())
        if len(elements) > 128 or root.tag != f"{{{_SVG_NAMESPACE}}}svg":
            raise CatalogValidationError(f"unsafe SVG content: {asset_path}")
        for element in elements:
            prefix = f"{{{_SVG_NAMESPACE}}}"
            if not isinstance(element.tag, str) or not element.tag.startswith(prefix):
                raise CatalogValidationError(f"unsafe SVG content: {asset_path}")
            element_name = element.tag.removeprefix(prefix)
            if element_name not in _SVG_ALLOWED_ELEMENTS:
                raise CatalogValidationError(f"unsafe SVG content: {asset_path}")
            for attribute, value in element.attrib.items():
                if attribute not in _SVG_ALLOWED_ATTRIBUTES:
                    raise CatalogValidationError(f"unsafe SVG content: {asset_path}")
                normalized_value = value.casefold()
                if any(
                    marker in normalized_value
                    for marker in _SVG_FORBIDDEN_VALUE_MARKERS
                ) or any(character in value for character in "\r\n\0"):
                    raise CatalogValidationError(f"unsafe SVG content: {asset_path}")
            if element_name != "title" and element.text and element.text.strip():
                raise CatalogValidationError(f"unsafe SVG content: {asset_path}")

    def _validate_recipe_coverage(
        self,
        variant: McpConnectionVariant,
        recipes: tuple[McpSetupRecipe, ...],
    ) -> None:
        """Ensure every evaluator-available finite environment has a plan.

        Platform and shell are closed finite vocabularies. App versions are not,
        so every supported platform/shell pair needs a version-agnostic base
        recipe; exact-version recipes may still override it.
        """

        platforms: tuple[str | None, ...] = (
            tuple(variant.requires.platform)
            if variant.requires.platform is not None
            else (None, *_PLATFORMS)
        )
        shells: tuple[str | None, ...] = (
            tuple(variant.requires.shell)
            if variant.requires.shell is not None
            else (None, *_SHELLS)
        )
        for platform in platforms:
            for shell in shells:
                covered = any(
                    recipe.selectors.app_version is None
                    and recipe.selectors.platform in {None, platform}
                    and recipe.selectors.shell in {None, shell}
                    for recipe in recipes
                )
                if not covered:
                    raise CatalogValidationError(
                        "recipe coverage missing for variant "
                        f"{variant.id}: platform={platform or 'unspecified'}, "
                        f"shell={shell or 'unspecified'}"
                    )

    def _validate_recipe(
        self,
        recipe: McpSetupRecipe,
        *,
        variant: McpConnectionVariant,
        executable_ids: frozenset[str],
    ) -> None:
        requires = variant.requires
        if (
            recipe.selectors.platform is not None
            and requires.platform is not None
            and recipe.selectors.platform not in requires.platform
        ):
            raise CatalogValidationError(
                f"recipe {recipe.id} platform cannot satisfy {variant.id}"
            )
        if (
            recipe.selectors.shell is not None
            and requires.shell is not None
            and recipe.selectors.shell not in requires.shell
        ):
            raise CatalogValidationError(
                f"recipe {recipe.id} shell cannot satisfy {variant.id}"
            )

        uses_secret = False
        for artifact in recipe.artifacts:
            if isinstance(artifact, CommandArtifact):
                if artifact.executable_id not in executable_ids:
                    raise CatalogValidationError(
                        f"recipe {recipe.id} references missing executable "
                        f"{artifact.executable_id}"
                    )
                if not artifact.argv or any(not argument for argument in artifact.argv):
                    raise CatalogValidationError(
                        f"recipe {recipe.id} has an empty command argument"
                    )
                rendered_literals = {
                    "".join(
                        atom.literal
                        for atom in argument
                        if isinstance(atom, LiteralAtom)
                    ).casefold()
                    for argument in artifact.argv
                }
                if rendered_literals & _FORBIDDEN_COMMAND_STRING_ARGUMENTS:
                    raise CatalogValidationError(
                        f"recipe {recipe.id} uses command-string mode"
                    )
                for argument in artifact.argv:
                    argument_uses_secret = any(
                        isinstance(atom, AccessTokenAtom) for atom in argument
                    )
                    if (
                        argument_uses_secret
                        and not is_approved_command_argv_secret_sink(argument)
                    ):
                        raise CatalogValidationError(
                            f"recipe {recipe.id} uses an unapproved command secret sink"
                        )
                    uses_secret = uses_secret or argument_uses_secret
                for env_name, atoms in (artifact.env or {}).items():
                    if not is_safe_command_env_name(env_name):
                        raise CatalogValidationError(
                            f"recipe {recipe.id} uses an unsafe command "
                            "environment name"
                        )
                    env_uses_secret = any(
                        isinstance(atom, AccessTokenAtom) for atom in atoms
                    )
                    if env_uses_secret and not is_approved_command_env_secret_sink(
                        env_name, atoms
                    ):
                        raise CatalogValidationError(
                            f"recipe {recipe.id} uses an unapproved command secret sink"
                        )
                    uses_secret = uses_secret or env_uses_secret
            elif isinstance(artifact, AppCommandArtifact):
                # An in-app prompt is not a shell, so this kind bypasses
                # the executable registry by design. Pinning it to
                # slash-command form on a single line keeps that bypass
                # from becoming a way to ship a shell invocation as
                # copyable setup text.
                first = artifact.value[0]
                if not isinstance(first, LiteralAtom) or not (
                    is_slash_command_shaped(first.literal)
                ):
                    raise CatalogValidationError(
                        f"recipe {recipe.id} in-app command must be a slash command"
                    )
                if any(
                    any(ch in atom.literal for ch in "\r\n\x00")
                    for atom in artifact.value
                    if isinstance(atom, LiteralAtom)
                ):
                    raise CatalogValidationError(
                        f"recipe {recipe.id} in-app command must be a single line"
                    )
                if any(
                    has_unsafe_control_characters(atom.literal)
                    for atom in artifact.value
                    if isinstance(atom, LiteralAtom)
                ):
                    raise CatalogValidationError(
                        f"recipe {recipe.id} in-app command must not contain "
                        "control characters"
                    )
            elif isinstance(artifact, JsonConfigArtifact):
                uses_secret = self._validate_json_template(
                    artifact.value, recipe_id=recipe.id
                ) or uses_secret
            elif isinstance(artifact, DeepLinkArtifact):
                literal_prefix = "".join(
                    atom.literal
                    for atom in artifact.url
                    if isinstance(atom, LiteralAtom)
                )
                if literal_prefix and not any(
                    isinstance(atom, McpUrlAtom) for atom in artifact.url
                ):
                    parsed = urlparse(literal_prefix)
                    if parsed.scheme != "https" or not parsed.netloc:
                        raise CatalogValidationError(
                            f"recipe {recipe.id} deep link must use public HTTPS"
                        )

        has_authenticate_step = any(
            artifact.step == "authenticate" for artifact in recipe.artifacts
        )
        if variant.auth == "oauth" and not has_authenticate_step:
            raise CatalogValidationError(
                f"OAuth recipe {recipe.id} must ship an authenticate-step artifact"
            )
        if variant.auth == "access-key" and has_authenticate_step:
            raise CatalogValidationError(
                f"Access Key recipe {recipe.id} must not ship an "
                "authenticate-step artifact"
            )
        if variant.auth == "oauth" and uses_secret:
            raise CatalogValidationError(
                f"OAuth recipe {recipe.id} must not request an access token"
            )
        if variant.auth == "access-key" and not uses_secret:
            raise CatalogValidationError(
                f"Access Key recipe {recipe.id} has no approved secret sink"
            )

    def _validate_json_template(
        self,
        value,
        *,
        recipe_id: str,
        path: tuple[str, ...] = (),
    ) -> bool:
        if value is None or isinstance(value, (bool, int, float, str)):
            return False
        if isinstance(value, list):
            uses_secret = False
            for index, item in enumerate(value):
                item_uses_secret = self._validate_json_template(
                    item,
                    recipe_id=recipe_id,
                    path=(*path, str(index)),
                )
                uses_secret = uses_secret or item_uses_secret
            return uses_secret
        if not isinstance(value, dict):
            raise CatalogValidationError(
                f"recipe {recipe_id} JSON template contains a non-JSON value"
            )
        if path and "bearerToken" in value:
            raise CatalogValidationError(
                f"recipe {recipe_id} uses a nested bearerToken field"
            )
        if "slot" in value or "literal" in value:
            try:
                atom = _SECRET_ATOM_ADAPTER.validate_python(value)
            except ValidationError as exc:
                raise CatalogValidationError(
                    f"recipe {recipe_id} JSON template has an invalid atom"
                ) from exc
            if isinstance(atom, AccessTokenAtom):
                if not is_approved_json_access_token_sink(path, atom.slot):
                    raise CatalogValidationError(
                        f"recipe {recipe_id} uses an unapproved JSON secret sink"
                    )
                return True
            return False
        uses_secret = False
        for key, item in value.items():
            item_uses_secret = self._validate_json_template(
                item,
                recipe_id=recipe_id,
                path=(*path, str(key)),
            )
            uses_secret = uses_secret or item_uses_secret
        return uses_secret

    def _read_text(self, relative_path: str) -> str:
        resource = self._resource_root.joinpath(*PurePosixPath(relative_path).parts)
        return resource.read_text(encoding="utf-8")

    def _read_bytes(self, relative_path: str) -> bytes:
        resource = self._resource_root.joinpath(*PurePosixPath(relative_path).parts)
        return resource.read_bytes()


def _require_unique(kind: str, values: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        normalized = value.casefold()
        if normalized in seen:
            raise CatalogValidationError(f"duplicate {kind}: {value}")
        seen.add(normalized)


def _reject_non_json_constant(value: str):
    raise ValueError(f"non-standard JSON constant is forbidden: {value}")


@lru_cache(maxsize=1)
def get_mcp_app_catalog_repository() -> CatalogRepository:
    """Return the process-wide immutable repository after first validation."""

    repository = CatalogRepository()
    repository.snapshot()
    return repository


__all__ = [
    "CatalogRepository",
    "CatalogValidationError",
    "VerifiedOAuthAssociation",
    "get_mcp_app_catalog_repository",
]
