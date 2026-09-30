"""Select a versioned symbolic setup recipe without handling secret values."""

from __future__ import annotations

from dataclasses import dataclass

from backend.catalog.mcp_apps.compatibility import SetupContext
from backend.catalog.mcp_apps.models import McpConnectionVariant, SetupArtifact
from backend.catalog.mcp_apps.repository import CatalogRepository


class SetupPlanningError(RuntimeError):
    error_code = "mcp_setup_plan_unavailable"


@dataclass(frozen=True)
class SetupPlan:
    catalog_revision: str
    owner_kind: str
    owner_id: str
    variant_id: str
    recipe_id: str
    recipe_version: int
    auth: str
    transport: str
    surface: str
    default_scope_preset: str
    artifacts: tuple[SetupArtifact, ...]
    verification_instruction: str


class SetupPlanner:
    """Narrow planner port shared by HTTP and CLI adapters."""

    def __init__(self, repository: CatalogRepository) -> None:
        self.repository = repository

    def plan_for_app(
        self,
        *,
        app_id: str,
        variant_id: str,
        context: SetupContext,
        locale: str = "en",
    ) -> SetupPlan:
        app = self.repository.find_app(app_id)
        if app is None or app.lifecycle != "active":
            raise SetupPlanningError("catalog app is unavailable")
        variant = _find_owned_variant(app.variants, variant_id)
        return self._plan(
            owner_kind="app",
            owner_id=app.id,
            variant=variant,
            context=context,
            locale=locale,
        )

    def plan_for_generic_profile(
        self,
        *,
        profile_id: str,
        variant_id: str,
        context: SetupContext,
        locale: str = "en",
    ) -> SetupPlan:
        profile = self.repository.find_generic_profile(profile_id)
        if profile is None:
            raise SetupPlanningError("generic MCP profile is unavailable")
        variant = _find_owned_variant(profile.variants, variant_id)
        return self._plan(
            owner_kind="generic-profile",
            owner_id=profile.id,
            variant=variant,
            context=context,
            locale=locale,
        )

    def _plan(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        variant: McpConnectionVariant,
        context: SetupContext,
        locale: str,
    ) -> SetupPlan:
        candidates = []
        for recipe_id in variant.recipe_ids:
            recipe = self.repository.find_recipe(recipe_id)
            if recipe is not None and _selectors_match(recipe.selectors, context):
                specificity = sum(
                    value is not None
                    for value in (
                        recipe.selectors.platform,
                        recipe.selectors.shell,
                        recipe.selectors.app_version,
                    )
                )
                candidates.append((specificity, recipe.version, recipe))
        if not candidates:
            raise SetupPlanningError("no setup recipe matches this environment")
        candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
        best_specificity = candidates[0][0]
        best_version = candidates[0][1]
        tied = [
            row
            for row in candidates
            if row[0] == best_specificity and row[1] == best_version
        ]
        if len(tied) != 1:
            raise SetupPlanningError("setup recipe selection is ambiguous")
        recipe = tied[0][2]
        instruction = (
            recipe.verify.instruction.ko
            if locale == "ko"
            else recipe.verify.instruction.en
        )
        return SetupPlan(
            catalog_revision=self.repository.snapshot().catalog_revision,
            owner_kind=owner_kind,
            owner_id=owner_id,
            variant_id=variant.id,
            recipe_id=recipe.id,
            recipe_version=recipe.version,
            auth=variant.auth,
            transport=variant.transport,
            surface=variant.surface,
            default_scope_preset=variant.default_scope_preset,
            artifacts=recipe.artifacts,
            verification_instruction=instruction,
        )


def _find_owned_variant(
    variants: tuple[McpConnectionVariant, ...], variant_id: str
) -> McpConnectionVariant:
    normalized = variant_id.strip().casefold()
    variant = next((item for item in variants if item.id == normalized), None)
    if variant is None:
        raise SetupPlanningError("variant does not belong to the selected app")
    return variant


def _selectors_match(selectors, context: SetupContext) -> bool:
    return (
        (selectors.platform is None or selectors.platform == context.platform)
        and (selectors.shell is None or selectors.shell == context.shell)
        and (
            selectors.app_version is None
            or selectors.app_version == context.app_version
        )
    )


__all__ = ["SetupPlan", "SetupPlanner", "SetupPlanningError"]
