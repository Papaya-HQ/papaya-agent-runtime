"""Tiny JSON-schema subset validator (zero dependencies).

Supports the constructs used by this project's schemas: ``type`` (incl. unions
and ``null``), ``required``, ``enum``, ``additionalProperties: false``,
``properties``, ``items``, ``minLength``, ``minItems``, and ``minimum``. This is
deliberately minimal; it is not a general Draft 2020-12 implementation.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import papaya_agent_runtime

_SCHEMA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(papaya_agent_runtime.__file__))), "schemas"
)

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


class SchemaError(Exception):
    pass


@lru_cache(maxsize=16)
def load_schema(name: str) -> dict:
    path = os.path.join(_SCHEMA_DIR, name)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _check_type(value: object, types: object, path: str) -> None:
    names = [types] if isinstance(types, str) else list(types)
    # bool is a subclass of int in Python; guard against accidental matches.
    for name in names:
        py = _TYPE_MAP[name]
        if name == "integer" and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return
    raise SchemaError(f"{path}: expected type {names}, got {type(value).__name__}")


def validate(instance: object, schema: dict, path: str = "$") -> None:
    if "type" in schema:
        _check_type(instance, schema["type"], path)

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path}: {instance!r} not in enum {schema['enum']}")

    if isinstance(instance, str) and "minLength" in schema and len(instance) < schema["minLength"]:
        raise SchemaError(f"{path}: shorter than minLength {schema['minLength']}")

    if (
        isinstance(instance, (int, float))
        and not isinstance(instance, bool)
        and "minimum" in schema
        and instance < schema["minimum"]
    ):
        raise SchemaError(f"{path}: below minimum {schema['minimum']}")

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                raise SchemaError(f"{path}: missing required property {req!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            if extra:
                raise SchemaError(f"{path}: unexpected properties {sorted(extra)}")
        for key, subschema in props.items():
            if key in instance:
                validate(instance[key], subschema, f"{path}.{key}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaError(f"{path}: fewer than minItems {schema['minItems']}")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                validate(item, item_schema, f"{path}[{i}]")


def validate_task(packet: dict) -> None:
    validate(packet, load_schema("task.schema.json"))


def validate_worker_result(result: dict) -> None:
    validate(result, load_schema("worker-result.schema.json"))
