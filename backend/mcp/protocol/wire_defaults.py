"""Leave fields at their declared default off the wire, for every tool.

A tool result's model declares defaults: ``false``, ``0``, an empty list or
object, ``null``. Repeating them on every call and on every list item is pure
token cost (a 200-block structure map carried about 12 KB of empty table
fields). This walks the wire copy alongside the tool's output model and drops
a field whose value equals its declared default when that default is empty
(``null``, ``false``, ``0``, ``""``, ``[]``, ``{}``), so an absent field means
false, 0, empty or null. Server instructions say so once, since outputSchema
is not advertised.

What it never drops:

* a required field (no declared default);
* a field whose default is a real value rather than an empty one: a begin
  response's ``editing_revision`` defaults to 1, and 1 is the CAS input an
  agent must echo, not an absence;
* a field marked ``WIRE_EXPLICIT_FIELD``: null there means "unknown", which
  an absent field would blur;
* anything inside free-form JSON (``dict[str, Any]`` values) or an ambiguous
  union of models, and never an array element.

The comparison is against the wire dict's value, not the model's attribute:
the runner appends warnings to the dumped envelope, and redaction rewrites
values, after the model was built.
"""

from __future__ import annotations

import types
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from backend.schemas.common import WIRE_EXPLICIT_FIELD

# Defaults that mean "nothing here", already in their JSON form. A default
# outside this set (1, "none", a non-empty list) is a real value and always
# stays on the wire.
_EMPTY_DEFAULTS = (None, False, 0, "", [], {})


def omit_declared_defaults(model: type[BaseModel], data: Any) -> Any:
    """``data`` (a dumped ``model``) without fields at their declared default.

    A ``model`` that is not a pydantic model (an in-process stub) leaves
    ``data`` as it is.
    """

    if not isinstance(data, dict) or not (
        isinstance(model, type) and issubclass(model, BaseModel)
    ):
        return data
    fields = model.model_fields
    kept: dict[str, Any] = {}
    for key, value in data.items():
        field = fields.get(key)
        if field is None:
            kept[key] = value
            continue
        explicit = isinstance(field.json_schema_extra, dict) and all(
            field.json_schema_extra.get(name) == flag
            for name, flag in WIRE_EXPLICIT_FIELD.items()
        )
        if not explicit and not field.is_required():
            default = field.get_default(call_default_factory=True)
            if default in _EMPTY_DEFAULTS and value == default:
                continue
        kept[key] = _nested(field.annotation, value)
    return kept


def _nested(annotation: Any, value: Any) -> Any:
    item_model = _single_model(annotation)
    if item_model is not None:
        return omit_declared_defaults(item_model, value)
    list_model = _list_item_model(annotation)
    if list_model is not None and isinstance(value, list):
        return [omit_declared_defaults(list_model, item) for item in value]
    return value


def _without_none(annotation: Any) -> list[Any]:
    if get_origin(annotation) in (Union, types.UnionType):
        return [arg for arg in get_args(annotation) if arg is not type(None)]
    return [annotation]


def _single_model(annotation: Any) -> type[BaseModel] | None:
    options = _without_none(annotation)
    if len(options) == 1 and isinstance(options[0], type):
        if issubclass(options[0], BaseModel):
            return options[0]
    return None


def _list_item_model(annotation: Any) -> type[BaseModel] | None:
    options = _without_none(annotation)
    if len(options) != 1 or get_origin(options[0]) not in (list, tuple):
        return None
    args = [arg for arg in get_args(options[0]) if arg is not Ellipsis]
    return _single_model(args[0]) if len(args) == 1 else None
