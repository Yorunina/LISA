"""Shared wire schemas and strict parsing for paid structured model results."""

import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache

from pydantic import BaseModel, ValidationError


@dataclass(frozen=True)
class StructuredSchema:
    json_text: str
    fingerprint: str


def _compact_schema(value):
    if isinstance(value, dict):
        result = {
            key: _compact_schema(item)
            for key, item in value.items()
            if key != "default" and (key != "title" or not isinstance(item, str))
        }
        if result.get("type") == "object":
            result["required"] = list(result.get("properties", {}))
            result["additionalProperties"] = False
        return result
    if isinstance(value, list):
        return [_compact_schema(item) for item in value]
    return value


@lru_cache(maxsize=128)
def structured_schema(schema: type[BaseModel]) -> StructuredSchema:
    """Cache immutable schema text; clear the cache after an explicit model rebuild."""
    encoded = json.dumps(
        _compact_schema(schema.model_json_schema()), ensure_ascii=False, separators=(",", ":")
    )
    return StructuredSchema(encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest())


def parse_structured_output(schema: type[BaseModel], raw: str) -> BaseModel:
    """Keep internal defaults, but require every field in a provider's wire result."""
    result = schema.model_validate_json(raw, strict=True, extra="forbid")
    issues = []

    def visit(value, location):
        if isinstance(value, BaseModel):
            for name in type(value).model_fields:
                path = (*location, name)
                if name not in value.model_fields_set:
                    issues.append({"type": "missing", "loc": path, "input": None})
                else:
                    visit(getattr(value, name), path)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                visit(item, (*location, index))
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(item, (*location, key))
        elif isinstance(value, float) and not math.isfinite(value):
            issues.append({"type": "finite_number", "loc": location, "input": None})

    visit(result, ())
    if issues:
        raise ValidationError.from_exception_data(schema.__name__, issues)
    return result


def validation_error_code(failures: list[dict]) -> str:
    """Return fixed diagnostic codes; never include provider content or field values."""
    types = {row["type"] for row in failures}
    if "json_invalid" in types:
        return "output_json_invalid"
    if "missing" in types:
        return "output_missing_fields"
    if "extra_forbidden" in types:
        return "output_extra_fields"
    if any(kind.endswith(("_type", "_parsing")) for kind in types):
        return "output_type_invalid"
    return "output_constraint_invalid"
