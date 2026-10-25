"""Startup check: fields.yaml must match the pydantic schema exactly.

Fails fast with actionable messages when a field is missing, extra, or has a
kind incompatible with its annotation. See docs/CHANGING_THE_SCHEMA.md.
"""

from __future__ import annotations

import types
from datetime import date
from enum import Enum
from typing import Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from normalizer_agent.settings import FieldsConfig

LIST_KINDS = {"vocab_list", "enum_list", "free_text_list"}
INTEGER_KINDS = {"integer", "integer_range_min", "integer_range_max", "money"}
SINGLE_KINDS = {
    "passthrough",
    "vocab_single",
    "enum_single",
    "integer",
    "integer_range_min",
    "integer_range_max",
    "money",
    "date",
    "location",
}


class FieldsConfigMismatchError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "fields.yaml does not match the schema model:\n  - " + "\n  - ".join(problems)
        )


def _unwrap_optional(annotation: object) -> object:
    if get_origin(annotation) is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        return args[0] if len(args) == 1 else annotation
    if get_origin(annotation) is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        return args[0] if len(args) == 1 else annotation
    return annotation


def _is_list(annotation: object) -> bool:
    return get_origin(_unwrap_optional(annotation)) is list


def _is_int(annotation: object) -> bool:
    return _unwrap_optional(annotation) in {int, float}


def _is_date(annotation: object) -> bool:
    unwrapped = _unwrap_optional(annotation)
    return unwrapped is date


def _is_enum(annotation: object) -> bool:
    unwrapped = _unwrap_optional(annotation)
    return isinstance(unwrapped, type) and issubclass(unwrapped, Enum)


def _is_str(annotation: object) -> bool:
    return _unwrap_optional(annotation) is str


def check_fields_config(fields: FieldsConfig, model: type[BaseModel]) -> None:
    """Raise FieldsConfigMismatchError listing every inconsistency."""

    problems: list[str] = []
    model_fields: dict[str, FieldInfo] = model.model_fields
    yaml_names = set(fields.fields)
    model_names = set(model_fields)

    for missing in sorted(model_names - yaml_names):
        problems.append(f"field '{missing}' exists in the schema but is missing from fields.yaml")
    for extra in sorted(yaml_names - model_names):
        problems.append(f"field '{extra}' is defined in fields.yaml but not in the schema")

    for name in sorted(model_names & yaml_names):
        rule = fields.fields[name]
        annotation = model_fields[name].annotation
        if _is_list(annotation):
            if rule.kind not in LIST_KINDS:
                problems.append(
                    f"field '{name}' is a list in the schema but has kind '{rule.kind}' "
                    f"(expected one of {sorted(LIST_KINDS)})"
                )
            item_annotation = get_args(_unwrap_optional(annotation))[0]
            if _is_enum(item_annotation) and rule.kind != "enum_list":
                problems.append(f"field '{name}' is an enum list; kind must be 'enum_list'")
        elif _is_enum(annotation):
            if rule.kind not in {"enum_list", "enum_single", "passthrough"}:
                problems.append(
                    f"field '{name}' is an enum; kind must be enum_list/enum_single/passthrough, "
                    f"got '{rule.kind}'"
                )
        elif _is_int(annotation):
            if rule.kind not in INTEGER_KINDS:
                problems.append(
                    f"field '{name}' is numeric; kind must be one of {sorted(INTEGER_KINDS)}, "
                    f"got '{rule.kind}'"
                )
        elif _is_date(annotation):
            if rule.kind not in {"date", "passthrough"}:
                problems.append(
                    f"field '{name}' is a date; kind must be 'date' or 'passthrough', "
                    f"got '{rule.kind}'"
                )
        elif _is_str(annotation):
            if rule.kind in LIST_KINDS:
                problems.append(
                    f"field '{name}' is a scalar string; kind '{rule.kind}' is a list kind"
                )
            if rule.kind == "money_currency" and name != "salary_currency":
                problems.append(
                    f"field '{name}' uses kind 'money_currency'; only 'salary_currency' may "
                    "(it is written by the money rule of its salary fields)"
                )

    if problems:
        raise FieldsConfigMismatchError(problems)
