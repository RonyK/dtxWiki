"""Shared card schemas and structural validation for delegated task answers."""

from copy import deepcopy
from functools import wraps

from jsonschema import Draft202012Validator

from backend.schemas.context_write import CONTEXT_PAGE_TYPES
from backend.schemas.page import ALIAS_MAX_LENGTH
from backend.schemas.read import SEARCH_QUERY_MAX_LENGTH

ANSWER_SCHEMAS = {
    "folder_nav_summary": {
        "type": "object",
        "properties": {
            "nav_summary": {"type": "string", "minLength": 1, "maxLength": 160},
            "negative_cues": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
            },
        },
        "required": ["nav_summary"],
        "additionalProperties": False,
    },
    "coverage_gap_review": {
        "type": "object",
        "properties": {
            "verdict": {"enum": ["compile_candidate", "augment_existing", "dismiss"]},
            "rationale": {"type": "string", "maxLength": 500},
            "proposal": {
                "type": ["object", "null"],
                "properties": {
                    "title": {"type": "string"},
                    "outline": {"type": "string"},
                },
                "required": ["title", "outline"],
                "additionalProperties": False,
            },
            "page_id": {"type": ["string", "null"], "format": "uuid"},
        },
        "required": ["verdict", "rationale"],
        "additionalProperties": False,
    },
    "page_meta_refresh": {
        "type": "object",
        "properties": {
            "aliases": {
                "type": "array",
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": ALIAS_MAX_LENGTH,
                },
                "maxItems": 5,
            },
            "nav_description": {"type": "string", "maxLength": 200},
            "placement_hint": {"enum": ["keep", "move", "unsure", None]},
        },
        "required": ["aliases", "nav_description"],
        "additionalProperties": False,
    },
    "folder_overview_draft": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "outline": {
                "type": "array",
                "minItems": 3,
                "maxItems": 10,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
            },
            "body_md": {"type": "string", "minLength": 1},
        },
        "required": ["title", "outline", "body_md"],
        "additionalProperties": False,
    },
    "near_duplicate_verdict": {
        "type": "object",
        "properties": {
            "verdict": {"enum": ["merge", "distinct"]},
            "rationale": {"type": "string", "maxLength": 500},
            "negative_cues": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
            },
        },
        "required": ["verdict", "rationale"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"verdict": {"const": "distinct"}}},
                "then": {
                    "required": ["negative_cues"],
                    "properties": {"negative_cues": {"minItems": 1}},
                },
            }
        ],
    },
    "import_batch_classify": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "page_type": {"enum": list(CONTEXT_PAGE_TYPES)},
            "placement_hint": {"type": "string", "maxLength": 500},
        },
        "required": ["title", "page_type", "placement_hint"],
        "additionalProperties": False,
    },
    "stale_contradiction_check": {
        "type": "object",
        "properties": {
            "contradicts": {"enum": ["yes", "no", "unclear"]},
            "rationale": {"type": "string", "maxLength": 1000},
        },
        "required": ["contradicts", "rationale"],
        "additionalProperties": False,
    },
    "golden_query_candidates": {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": SEARCH_QUERY_MAX_LENGTH,
                        },
                        "expected_kind": {
                            "enum": ["context_page", "library_item", "agent_page"]
                        },
                        "expected_id": {"type": "string", "format": "uuid"},
                    },
                    "required": ["query", "expected_kind", "expected_id"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["queries"],
        "additionalProperties": False,
    },
}

# The existing normalizers enforce lengths after their own trimming and retain
# UUID parsing, defaults and dependent rules. Validate JSON structure first so
# malformed nested values cannot reach their string or membership operators.
_SHAPE_KEYS = frozenset(
    {"type", "enum", "const", "properties", "required", "additionalProperties", "items"}
)


def _structural_schema(schema):
    result = {key: value for key, value in schema.items() if key in _SHAPE_KEYS}
    if "properties" in result:
        result["properties"] = {
            name: _structural_schema(value)
            for name, value in result["properties"].items()
        }
    for key in ("items", "additionalProperties"):
        if isinstance(result.get(key), dict):
            result[key] = _structural_schema(result[key])
    return result


_VALIDATORS = {
    task_type: Draft202012Validator(_structural_schema(schema))
    for task_type, schema in ANSWER_SCHEMAS.items()
}


def answer_schema(task_type):
    return deepcopy(ANSWER_SCHEMAS[task_type])


def validate_answer(task_type):
    validator = _VALIDATORS[task_type]

    def decorate(normalize):
        @wraps(normalize)
        def validated(answer):
            return normalize(answer) if validator.is_valid(answer) else None

        return validated

    return decorate
