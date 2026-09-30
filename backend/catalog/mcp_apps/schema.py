"""Canonical JSON Schema projection for the Pydantic catalog vocabulary."""

from __future__ import annotations

from typing import Any

from backend.catalog.mcp_apps.models import McpAppCatalogV1

CATALOG_SCHEMA_ID = "https://dtxwiki.local/schemas/mcp-app-catalog.v1.json"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def catalog_json_schema() -> dict[str, Any]:
    # Pydantic remains the runtime shape authority. The checked-in JSON Schema
    # is generated from exactly this model, while repository cross-reference and
    # secret-sink checks intentionally remain stricter than structural schema.
    schema = McpAppCatalogV1.model_json_schema(by_alias=True, mode="validation")
    # ``JsonConfigArtifact.value`` is intentionally stored as ``Any`` in the
    # Pydantic model because it is a recursive JSON template. Project the
    # recursive closed vocabulary explicitly so the public JSON Schema rejects
    # unknown atom slots and secret placement just as the startup repository
    # validator does. Secrets are approved only at the root ``bearerToken`` key
    # or as a complete Bearer value at ``headers.Authorization``; adding another
    # sink is a reviewed schema/policy change.
    definitions = schema["$defs"]
    definitions["PublicJsonTemplateValue"] = _public_json_template_schema()
    definitions["JsonConfigTemplate"] = _json_config_template_schema()
    definitions["JsonConfigArtifact"]["properties"]["value"] = {
        "$ref": "#/$defs/JsonConfigTemplate"
    }
    definitions["CommandArtifact"]["properties"]["env"]["anyOf"][0][
        "propertyNames"
    ] = {"pattern": r"^[A-Z_][A-Z0-9_]*$"}
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["$id"] = CATALOG_SCHEMA_ID
    schema["title"] = "dtxWiki MCP application catalog v1"
    return schema


def _public_json_template_schema() -> dict[str, Any]:
    public_ref = {"$ref": "#/$defs/PublicJsonTemplateValue"}
    return {
        "anyOf": [
            {"type": ["string", "number", "boolean", "null"]},
            {"$ref": "#/$defs/LiteralAtom"},
            {"$ref": "#/$defs/McpUrlAtom"},
            {"$ref": "#/$defs/ServerNameAtom"},
            {"type": "array", "items": public_ref},
            {
                "type": "object",
                "propertyNames": {
                    "not": {"enum": ["literal", "slot", "bearerToken"]}
                },
                "additionalProperties": public_ref,
            },
        ]
    }


def _json_config_template_schema() -> dict[str, Any]:
    public_ref = {"$ref": "#/$defs/PublicJsonTemplateValue"}
    authorization_bearer_atom = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"slot": {"const": "authorization-bearer"}},
        "required": ["slot"],
    }
    headers_object = {
        "type": "object",
        "propertyNames": {"not": {"enum": ["literal", "slot", "bearerToken"]}},
        "properties": {
            "Authorization": {"anyOf": [public_ref, authorization_bearer_atom]}
        },
        "additionalProperties": public_ref,
    }
    return {
        "anyOf": [
            public_ref,
            {
                "type": "object",
                "propertyNames": {"not": {"enum": ["literal", "slot"]}},
                "properties": {
                    "bearerToken": {
                        "anyOf": [
                            public_ref,
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "slot": {"const": "access-token"}
                                },
                                "required": ["slot"],
                            },
                        ]
                    },
                    "headers": {"anyOf": [public_ref, headers_object]},
                },
                "additionalProperties": public_ref,
            },
        ]
    }


__all__ = ["catalog_json_schema"]
