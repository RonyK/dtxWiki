"""Project a tool's Pydantic input schema onto what agent hosts can read.

The server validates arguments against the full Pydantic model -- discriminated
unions, ``$ref``/``$defs``, ``const`` and all. Hosts do not: they convert
``inputSchema`` into their own function-parameter format first, and the common
converters support only a small subset of JSON Schema. openai/codex#13746 shows
an ``items: {"$ref": ...}`` array reaching the model as ``string[]``; Gemini's
function declarations reject ``const`` and ``oneOf``; several providers reject
every composition keyword. ``patch_context_edit``'s operations reached one
agent as ``Array<unknown>``, so it could not see a single operation field.

The projection keeps every field and every allowed value while using only
keywords every host reads: ``$ref`` is inlined, ``const`` becomes a one-value
``enum``, an optional ``X | None`` becomes ``X`` (omission already means
"none"), and a union of object variants becomes ONE object whose
discriminator is an ``enum`` of every variant and whose description spells out
which fields each variant requires. Per-variant ``required`` is the one thing a
flat object cannot express; the server still enforces it and names the missing
fields in ``error_details``.
"""

from __future__ import annotations

import copy
from typing import Any

_COMPOSITION_KEYS = ("oneOf", "anyOf")
_NON_PORTABLE_KEYS = frozenset(
    {"$ref", "$defs", "oneOf", "anyOf", "allOf", "discriminator", "const"}
)
# OpenAPI's `discriminator` is not JSON Schema; `title` is noise to a model.
_DROPPED_KEYS = {"discriminator", "title", "$defs"}


def host_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.get("$defs", {})
    inlined = _inline(schema, defs, ())
    return _simplify(inlined)


def _inline(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_inline(item, defs, stack) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        if name in stack:
            raise ValueError(f"recursive schema reference {name!r} cannot be inlined")
        target = _inline(copy.deepcopy(defs[name]), defs, (*stack, name))
        siblings = {
            key: _inline(value, defs, stack)
            for key, value in node.items()
            if key != "$ref"
        }
        return {**target, **siblings}
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROPPED_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            # Keys here are field names -- a field may be called "title".
            result[key] = {
                name: _inline(child, defs, stack) for name, child in value.items()
            }
        else:
            result[key] = _inline(value, defs, stack)
    return result


def _simplify(node: Any) -> Any:
    if isinstance(node, list):
        return [_simplify(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {
        key: (
            {name: _simplify(child) for name, child in value.items()}
            if key == "properties" and isinstance(value, dict)
            else _simplify(value)
        )
        for key, value in node.items()
    }
    if "const" in node:
        node["enum"] = [node.pop("const")]
    for key in _COMPOSITION_KEYS:
        if key in node:
            variants = node.pop(key)
            node = _merge_into(node, _collapse_union(variants))
    return node


def _collapse_union(variants: list[Any]) -> dict[str, Any]:
    concrete = [
        variant
        for variant in variants
        if not (isinstance(variant, dict) and variant.get("type") == "null")
    ]
    if len(concrete) == 1:
        return concrete[0]
    if concrete and all(_is_object(variant) for variant in concrete):
        return _merge_object_variants(concrete)
    types = sorted(
        {variant.get("type") for variant in concrete if isinstance(variant, dict)}
        - {None}
    )
    if len(types) == 1:
        return {"type": types[0]}
    # Mixed scalar unions do not occur in the tool catalog today; describe
    # rather than guess, so a future one is visible instead of mistyped.
    return {"description": "One of: " + ", ".join(str(t) for t in types)}


def _is_object(node: Any) -> bool:
    return isinstance(node, dict) and (
        node.get("type") == "object" or "properties" in node
    )


def _discriminator(variants: list[dict[str, Any]]) -> str | None:
    shared = set.intersection(
        *(set(variant.get("properties", {})) for variant in variants)
    )
    for name in sorted(shared):
        values = [variant["properties"][name].get("enum") for variant in variants]
        if all(isinstance(value, list) and len(value) == 1 for value in values):
            return name
    return None


def _merge_object_variants(variants: list[dict[str, Any]]) -> dict[str, Any]:
    tag = _discriminator(variants)
    labels = [
        str(variant["properties"][tag]["enum"][0]) if tag else f"variant {index + 1}"
        for index, variant in enumerate(variants)
    ]
    required_everywhere = set.intersection(
        *(set(variant.get("required", [])) for variant in variants)
    )
    properties: dict[str, dict[str, Any]] = {}
    users: dict[str, list[str]] = {}
    requirers: dict[str, list[str]] = {}
    for label, variant in zip(labels, variants, strict=True):
        required = set(variant.get("required", []))
        for name, child in variant.get("properties", {}).items():
            users.setdefault(name, []).append(label)
            if name in required:
                requirers.setdefault(name, []).append(label)
            if name == tag:
                continue
            properties[name] = (
                _merge_property(properties[name], child)
                if name in properties
                else copy.deepcopy(child)
            )

    for name, child in properties.items():
        if name in required_everywhere:
            continue
        used_by = users[name]
        note = (
            f"Used by: {', '.join(used_by)}."
            if len(used_by) < len(variants)
            else "Used by every variant."
        )
        needed = requirers.get(name, [])
        if needed:
            note += f" Required for: {', '.join(needed)}."
        child["description"] = (
            f"{child['description']} {note}" if child.get("description") else note
        )

    merged: dict[str, Any] = {"type": "object", "properties": {}}
    if tag is not None:
        recipe = " | ".join(
            _recipe(label, variant, tag)
            for label, variant in zip(labels, variants, strict=True)
        )
        merged["properties"][tag] = {
            "type": "string",
            "enum": labels,
            "description": (
                f"Which variant this is. Each variant needs its own fields: {recipe}"
            ),
        }
    merged["properties"].update(properties)
    merged["required"] = sorted(required_everywhere)
    if all(variant.get("additionalProperties") is False for variant in variants):
        merged["additionalProperties"] = False
    return merged


def _recipe(label: str, variant: dict[str, Any], tag: str) -> str:
    required = [name for name in variant.get("required", []) if name != tag]
    optional = [
        name
        for name in variant.get("properties", {})
        if name != tag and name not in required
    ]
    parts = [f"requires {', '.join(required)}" if required else "no required fields"]
    if optional:
        parts.append(f"optional {', '.join(optional)}")
    return f"{label} ({'; '.join(parts)})"


# Upper bounds survive a merge at their strictest declared value: advertising
# no limit at all let a model send a body the server then rejects. Lower
# bounds survive only when every variant declares one, at the loosest value:
# replace_text's new_text may be empty (a deletion) even though insert_text's
# may not, and a merged minLength of 1 would forbid that valid call.
_UPPER_BOUNDS = ("maxLength", "maxItems", "maximum")
_LOWER_BOUNDS = ("minLength", "minItems", "minimum")


def _merge_property(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    if left == right:
        return left
    merged: dict[str, Any] = {}
    for key in left.keys() & right.keys():
        if key == "description" or key in _UPPER_BOUNDS or key in _LOWER_BOUNDS:
            continue
        if left[key] == right[key]:
            merged[key] = left[key]
        elif key == "enum" and _both(left[key], right[key], list):
            merged[key] = list(dict.fromkeys([*left[key], *right[key]]))
        elif key == "required" and _both(left[key], right[key], list):
            # Only what every side requires is required of the merged object.
            merged[key] = [name for name in left[key] if name in right[key]]
        elif key == "properties" and _both(left[key], right[key], dict):
            # Field by field: two operations' `at` objects both carry `by`,
            # and a shallow merge kept only one side's variants -- the block
            # address vanished from the advertised enum.
            merged[key] = {
                name: (
                    _merge_property(left[key][name], right[key][name])
                    if name in left[key] and name in right[key]
                    else copy.deepcopy(left[key].get(name, right[key].get(name)))
                )
                for name in dict.fromkeys([*left[key], *right[key]])
            }
    for key in _UPPER_BOUNDS:
        values = [side[key] for side in (left, right) if key in side]
        if values:
            merged[key] = min(values)
    for key in _LOWER_BOUNDS:
        if key in left and key in right:
            merged[key] = min(left[key], right[key])
    description = _merge_descriptions(left.get("description"), right.get("description"))
    if description:
        merged["description"] = description
    return merged


_RECIPE_PREFIX = "Which variant this is. Each variant needs its own fields: "


def _merge_descriptions(left: str | None, right: str | None) -> str | None:
    # Two discriminators' recipes describe different variant sets; keep every
    # variant's line. Any other description: the first one wins.
    if (
        left
        and right
        and left.startswith(_RECIPE_PREFIX)
        and right.startswith(_RECIPE_PREFIX)
    ):
        parts = [
            *left.removeprefix(_RECIPE_PREFIX).split(" | "),
            *right.removeprefix(_RECIPE_PREFIX).split(" | "),
        ]
        return _RECIPE_PREFIX + " | ".join(dict.fromkeys(parts))
    return left or right


def _both(left: Any, right: Any, kind: type) -> bool:
    return isinstance(left, kind) and isinstance(right, kind)


def _merge_into(node: dict[str, Any], collapsed: dict[str, Any]) -> dict[str, Any]:
    # Keys written beside the union (description, default) describe the field
    # itself and win over the variant's own.
    return {**collapsed, **node}


def schema_uses_only_portable_keywords(schema: Any) -> bool:
    """True when no host-hostile keyword remains anywhere in ``schema``."""

    if isinstance(schema, list):
        return all(schema_uses_only_portable_keywords(item) for item in schema)
    if not isinstance(schema, dict):
        return True
    for key, value in schema.items():
        if key in _NON_PORTABLE_KEYS:
            return False
        if key == "properties" and isinstance(value, dict):
            if not all(schema_uses_only_portable_keywords(v) for v in value.values()):
                return False
        elif not schema_uses_only_portable_keywords(value):
            return False
    return True
