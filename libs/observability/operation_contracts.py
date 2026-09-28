"""Bounded, versioned contracts for structured domain-span observations.

Only metadata belongs in ``OperationRecord``. Raw candidate content stays in a
separate, short-lived object passed to an in-process evidence observer.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from types import MappingProxyType
from typing import Any, Literal, Mapping


Destination = Literal["otel_safe", "evidence_only", "derived_digest"]
_DESTINATIONS = frozenset({"otel_safe", "evidence_only", "derived_digest"})
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_FIELDS = 16
_MAX_CONTENT_NAMES = 16


@dataclass(frozen=True)
class FieldRule:
    value_type: type
    destinations: frozenset[Destination]
    max_length: int | None = None

    def __post_init__(self) -> None:
        if self.value_type not in (str, int, float, bool):
            raise ValueError("unsupported fact type")
        if not self.destinations or not self.destinations <= _DESTINATIONS:
            raise ValueError("invalid fact destinations")
        if self.max_length is not None and (
            self.value_type is not str or type(self.max_length) is not int
            or not 0 < self.max_length <= (128 if 'otel_safe' in self.destinations else 1024)
        ):
            raise ValueError("invalid fact length bound")


@dataclass(frozen=True)
class PhaseRule:
    facts: Mapping[str, FieldRule]
    content_names: frozenset[str]

    def __post_init__(self) -> None:
        if len(self.facts) > _MAX_FIELDS or len(self.content_names) > _MAX_CONTENT_NAMES:
            raise ValueError("schema has too many fields")
        if any(not _NAME.fullmatch(name) for name in (*self.facts, *self.content_names)):
            raise ValueError("invalid field name")
        if any(not isinstance(rule, FieldRule) for rule in self.facts.values()):
            raise ValueError("invalid field rule")
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))
        object.__setattr__(self, "content_names", frozenset(self.content_names))


@dataclass(frozen=True)
class OperationSchema:
    name: str
    version: int
    phases: Mapping[str, PhaseRule]

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name) or type(self.version) is not int or self.version <= 0:
            raise ValueError("invalid operation schema identity")
        if not self.phases or len(self.phases) > 16:
            raise ValueError("invalid operation phases")
        if any(not _NAME.fullmatch(name) for name in self.phases):
            raise ValueError("invalid phase name")
        if any(not isinstance(rule, PhaseRule) for rule in self.phases.values()):
            raise ValueError("invalid phase rule")
        object.__setattr__(self, "phases", MappingProxyType(dict(self.phases)))


_registry: dict[tuple[str, int], OperationSchema] = {}
_registry_lock = RLock()


def register_operation_schema(schema: OperationSchema) -> None:
    """Idempotently register an exact operation name and version."""
    if not isinstance(schema, OperationSchema):
        raise TypeError("operation schema required")
    key = schema.name, schema.version
    with _registry_lock:
        previous = _registry.get(key)
        if previous is not None and previous != schema:
            raise ValueError("conflicting schema registration")
        _registry[key] = schema


def get_operation_schema(name: str, version: int) -> OperationSchema | None:
    with _registry_lock:
        return _registry.get((name, version))


def validate_record(schema: OperationSchema, phase: str, facts: Mapping[str, Any],
                    content_names: tuple[str, ...] | list[str]) -> dict[str, str | int | float | bool]:
    """Validate before copying fields into any telemetry or evidence projection."""
    rule = schema.phases.get(phase)
    if rule is None:
        raise ValueError("unknown phase")
    if not isinstance(facts, Mapping) or len(facts) > _MAX_FIELDS:
        raise ValueError("too many facts")
    if len(content_names) > _MAX_CONTENT_NAMES or len(set(content_names)) != len(content_names):
        raise ValueError("invalid content names")
    if any(name not in rule.content_names for name in content_names):
        raise ValueError("unknown content")
    checked: dict[str, str | int | float | bool] = {}
    for name, value in facts.items():
        field_rule = rule.facts.get(name)
        if field_rule is None:
            raise ValueError("unknown fact")
        if type(value) is not field_rule.value_type:
            raise ValueError("invalid fact type")
        if isinstance(value, str) and len(value) > (field_rule.max_length or 128):
            raise ValueError("invalid fact length")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("invalid fact number")
        checked[name] = value
    return checked


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    parent_operation_id: str | None
    name: str
    schema_version: int
    phase: str
    sequence: int
    timestamp: datetime
    facts: Mapping[str, str | int | float | bool]

    def __post_init__(self) -> None:
        if not self.operation_id or not _NAME.fullmatch(self.name) or not _NAME.fullmatch(self.phase):
            raise ValueError("invalid operation record")
        if type(self.schema_version) is not int or self.schema_version <= 0 or self.sequence <= 0:
            raise ValueError("invalid operation record position")
        if self.timestamp.tzinfo is None:
            raise ValueError("operation record requires timezone")
        if len(self.facts) > _MAX_FIELDS or any(
            not isinstance(name, str) or not _NAME.fullmatch(name)
            or type(value) not in (str, int, float, bool)
            or isinstance(value, str) and len(value) > 1024
            or isinstance(value, float) and not math.isfinite(value)
            for name, value in self.facts.items()
        ):
            raise ValueError("invalid operation fact")
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))


@dataclass(frozen=True)
class ContentCandidate:
    value: Any = field(repr=False)
    provenance: object | None = field(repr=False)
