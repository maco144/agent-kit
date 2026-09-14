"""OutputSpec: provider schemas from Python types, and answer parsing."""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import Annotated, Literal, TypedDict

import pytest
from pydantic import BaseModel, Field

from agent_kit.output import OutputParseError, OutputSpec


class Item(BaseModel):
    sku: str = Field(min_length=3, pattern="^A", description="stock unit")
    qty: int = Field(ge=1, default=1)


class Invoice(BaseModel):
    number: str
    note: str | None = None
    items: list[Item]
    main: Item = Field(description="primary line")


class Tagged(BaseModel):
    tags: dict[str, int] = {}


class Node(BaseModel):
    name: str
    children: list[Node] = []


class Cat(BaseModel):
    kind: Literal["cat"]


class Dog(BaseModel):
    kind: Literal["dog"]


class Pet(BaseModel):
    pet: Annotated[Cat | Dog, Field(discriminator="kind")]


class Color(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class Point:
    x: int


class Movie(TypedDict):
    title: str


def objects(node: object) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            found.append(node)
        for value in node.values():
            found.extend(objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(objects(value))
    return found


def keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in keys(v)}
    return set()


def test_model_schema_is_strict():
    spec = OutputSpec.from_type(Invoice)
    assert (spec.name, spec.wrapped, spec.native_compatible) == ("Invoice", False, True)
    for obj in objects(spec.json_schema):
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    assert spec.json_schema["required"] == ["number", "note", "items", "main"]
    assert not {"title", "default", "minLength", "pattern", "minimum"} & keys(spec.json_schema)


def test_constraints_move_to_description_and_are_still_enforced():
    spec = OutputSpec.from_type(Invoice)
    sku = spec.json_schema["$defs"]["Item"]["properties"]["sku"]
    assert sku["description"] == "stock unit (constraints: minLength=3, pattern=^A)"
    bad = {"number": "1", "note": None, "items": [{"sku": "b", "qty": 0}], "main": {"sku": "Axy", "qty": 1}}
    with pytest.raises(OutputParseError) as exc:
        spec.parse(json.dumps(bad))
    assert exc.value.errors.splitlines() == [
        "items.0.sku: String should have at least 3 characters",
        "items.0.qty: Input should be greater than or equal to 1",
    ]


def test_ref_with_siblings_is_inlined():
    main = OutputSpec.from_type(Invoice).json_schema["properties"]["main"]
    assert "$ref" not in main
    assert main["description"] == "primary line"
    assert main["additionalProperties"] is False


def test_discriminated_union_becomes_any_of():
    pet = OutputSpec.from_type(Pet).json_schema["properties"]["pet"]
    assert "oneOf" not in pet and "discriminator" not in pet
    assert len(pet["anyOf"]) == 2


@pytest.mark.parametrize(
    ("output_type", "answer", "value"),
    [
        (list[int], '{"result": [1, 2]}', [1, 2]),
        (Color, '{"result": "red"}', Color.RED),
        (int, '{"result": 7}', 7),
        (Literal["yes", "no"], '{"result": "no"}', "no"),
        (Cat | Dog, '{"result": {"kind": "dog"}}', Dog(kind="dog")),
    ],
)
def test_non_object_roots_are_wrapped(output_type, answer, value):
    spec = OutputSpec.from_type(output_type)
    assert spec.wrapped is True
    assert spec.json_schema["required"] == ["result"]
    assert spec.json_schema["additionalProperties"] is False
    assert spec.parse(answer) == value


def test_dataclass_and_typeddict():
    assert OutputSpec.from_type(Point).parse('{"x": 3}') == Point(x=3)
    assert OutputSpec.from_type(Movie).parse('{"title": "Heat"}') == {"title": "Heat"}
    assert OutputSpec.from_type(Point).wrapped is False


def test_open_dicts_and_recursion_are_not_native():
    assert OutputSpec.from_type(Tagged).native_compatible is False
    assert OutputSpec.from_type(dict[str, int]).native_compatible is False
    assert OutputSpec.from_type(Node).native_compatible is False
    assert OutputSpec.from_type(Node).parse('{"result": {"name": "a", "children": []}}') == Node(name="a")


def test_name_sanitised():
    assert OutputSpec.from_type(list[int]).name == "list"
    assert OutputSpec.from_type(Cat | Dog).name == "output"


def test_parse_accepts_fences():
    spec = OutputSpec.from_type(Point)
    fence = "`" * 3
    assert spec.parse(f'{fence}json\n{{"x": 1}}\n{fence}') == Point(x=1)
    assert spec.parse(f'  {fence}\n{{"x": 2}}\n{fence}  ') == Point(x=2)


def test_parse_errors():
    with pytest.raises(OutputParseError, match="response is not valid JSON"):
        OutputSpec.from_type(Point).parse("x is 1")
    with pytest.raises(OutputParseError, match='expected a JSON object with a "result" key'):
        OutputSpec.from_type(list[int]).parse("[1, 2]")


def test_errors_capped_at_20_lines():
    spec = OutputSpec.from_type(list[int])
    with pytest.raises(OutputParseError) as exc:
        spec.parse(json.dumps({"result": ["x"] * 30}))
    assert len(exc.value.errors.splitlines()) == 20


def test_instructions_embed_schema():
    spec = OutputSpec.from_type(Point)
    text = spec.instructions()
    assert text.startswith("Respond with only a JSON value that matches this JSON Schema")
    assert json.dumps(spec.json_schema, indent=2) in text
