import json
from datetime import datetime
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lisa.model_calls import _reply_request
from lisa.structured_output import parse_structured_output, structured_schema


class Child(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    count: int = 0


class Parent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    children: list[Child]
    optional: Child | None = None


@pytest.mark.parametrize("raw,location", [
    ({"children": []}, ("optional",)),
    ({"children": [{"text": "synthetic"}], "optional": None}, ("children", 0, "count")),
    ({"children": [], "optional": {"text": "synthetic"}}, ("optional", "count")),
])
def test_wire_requires_defaults_in_nested_objects_but_internal_construction_keeps_them(
    raw, location,
):
    assert Parent.model_validate(raw)
    with pytest.raises(ValidationError) as raised:
        parse_structured_output(Parent, json.dumps(raw))
    assert any(row["type"] == "missing" and row["loc"] == location
               for row in raised.value.errors())


@pytest.mark.parametrize("count", ["1", True, 1.5])
def test_wire_never_coerces_scalar_types(count):
    with pytest.raises(ValidationError):
        parse_structured_output(Child, json.dumps({"text": "synthetic", "count": count}))


def test_wire_forbids_unknown_fields_even_with_permissive_internal_models():
    class Permissive(BaseModel):
        text: str

    with pytest.raises(ValidationError) as raised:
        parse_structured_output(Permissive, '{"text":"synthetic","private_field":"secret"}')
    assert raised.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("raw", ['{"value":NaN}', '{"value":Infinity}'])
def test_wire_rejects_nonfinite_numbers(raw):
    class Numeric(BaseModel):
        value: float

    with pytest.raises(ValidationError) as raised:
        parse_structured_output(Numeric, raw)
    assert raised.value.errors()[0]["type"] == "finite_number"


def test_strict_json_keeps_uuid_dates_nullable_and_nested_models():
    class TypedOutput(Parent):
        id: UUID
        timestamp: datetime

    raw = {
        "id": "00000000-0000-0000-0000-000000000001", "timestamp": "2026-10-28T00:00:00Z",
        "children": [{"text": "synthetic", "count": 1}], "optional": None,
    }
    result = parse_structured_output(TypedOutput, json.dumps(raw))
    assert isinstance(result.id, UUID) and isinstance(result.timestamp, datetime)
    assert result.children[0].count == 1 and result.optional is None


def test_schema_cache_is_bounded_and_reused_by_request_measurement(settings, monkeypatch):
    class Output(BaseModel):
        value: str = Field(max_length=20)

    original = Output.model_json_schema
    calls = []

    def counted(cls):
        calls.append(cls)
        return original()

    monkeypatch.setattr(Output, "model_json_schema", classmethod(counted))
    for protocol in ("responses", "chat_completions", "responses"):
        cfg = settings.model_copy(update={"model_api_protocol": protocol})
        _reply_request(cfg, "synthetic", [], Output)
    assert calls == [Output]
    assert structured_schema.cache_info().maxsize == 128


def test_request_mutation_cannot_poison_cached_schema(settings):
    cfg = settings.model_copy(update={"model_api_protocol": "responses"})
    request = _reply_request(cfg, "synthetic", [], Parent)
    compiled = structured_schema(Parent)
    request["text"]["format"]["schema"]["$defs"]["Child"]["properties"].clear()
    again = _reply_request(cfg, "synthetic", [], Parent)
    assert "text" in again["text"]["format"]["schema"]["$defs"]["Child"]["properties"]
    assert structured_schema(Parent) == compiled
