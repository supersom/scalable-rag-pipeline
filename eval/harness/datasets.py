import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ACTIONS = ("direct_answer", "tool_use", "retrieve")
TOOLS = ("calculator", "web_search", "sandbox", "graph_search")
ORIGINS = ("hand", "synthetic", "golden")
LABEL_TYPES = ("source", "fuzzy")


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class RoutingItem:
    id: str
    query: str
    expected_action: str
    expected_tool: Optional[str]
    tags: tuple
    also_accept: tuple
    expected_tool_output: Optional[str]


@dataclass(frozen=True)
class QAItem:
    id: str
    query: str
    origin: str
    answerable: bool
    label_type: Optional[str]
    expected_sources: tuple
    reference_contexts: tuple
    calibration_sources: tuple
    ground_truth: Optional[str]
    tags: tuple


def file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fail(path, lineno, message):
    raise DatasetError(f"{path}:{lineno}: {message}")


def _read_jsonl(path):
    rows = []
    for lineno, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append((lineno, json.loads(line)))
        except json.JSONDecodeError as e:
            raise DatasetError(f"{path}:{lineno}: invalid JSON ({e.msg})") from e
    return rows


def _text(row, key, path, lineno):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        _fail(path, lineno, f"'{key}' is required and must be a non-empty string")
    return value


def _string_list(row, key, path, lineno):
    value = row.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        _fail(path, lineno, f"'{key}' must be a list of non-empty strings")
    return tuple(value)


def _check_action_tool(action, tool, path, lineno, where):
    if action not in ACTIONS:
        _fail(path, lineno, f"{where} action must be one of {ACTIONS}, got {action!r}")
    if action == "tool_use" and tool not in TOOLS:
        _fail(path, lineno, f"{where} tool must be one of {TOOLS} when action is tool_use, got {tool!r}")
    if action != "tool_use" and tool is not None:
        _fail(path, lineno, f"{where} tool must be null unless action is tool_use, got {tool!r}")


def _unique_id(row, seen, path, lineno):
    item_id = _text(row, "id", path, lineno)
    if item_id in seen:
        _fail(path, lineno, f"duplicate id {item_id!r}")
    seen.add(item_id)
    return item_id


def load_routing(path):
    items, seen = [], set()
    for lineno, row in _read_jsonl(path):
        item_id = _unique_id(row, seen, path, lineno)
        action, tool = row.get("expected_action"), row.get("expected_tool")
        _check_action_tool(action, tool, path, lineno, "expected")
        also = []
        for pair in row.get("also_accept") or []:
            if not isinstance(pair, list) or len(pair) != 2:
                _fail(path, lineno, "'also_accept' entries must be [action, tool] pairs")
            _check_action_tool(pair[0], pair[1], path, lineno, "also_accept")
            also.append((pair[0], pair[1]))
        output = row.get("expected_tool_output")
        if output is not None and not isinstance(output, str):
            _fail(path, lineno, "'expected_tool_output' must be a string")
        items.append(RoutingItem(
            id=item_id,
            query=_text(row, "query", path, lineno),
            expected_action=action,
            expected_tool=tool,
            tags=_string_list(row, "tags", path, lineno),
            also_accept=tuple(also),
            expected_tool_output=output,
        ))
    return items


def load_qa(path):
    items, seen = [], set()
    for lineno, row in _read_jsonl(path):
        item_id = _unique_id(row, seen, path, lineno)
        origin = row.get("origin")
        if origin not in ORIGINS:
            _fail(path, lineno, f"origin must be one of {ORIGINS}, got {origin!r}")
        answerable = row.get("answerable")
        if not isinstance(answerable, bool):
            _fail(path, lineno, "answerable must be true or false")
        label_type = row.get("label_type")
        sources = _string_list(row, "expected_sources", path, lineno)
        contexts = _string_list(row, "reference_contexts", path, lineno)
        calibration = _string_list(row, "calibration_sources", path, lineno)
        if answerable:
            if label_type not in LABEL_TYPES:
                _fail(path, lineno, f"label_type must be one of {LABEL_TYPES} for answerable items")
            if label_type == "source":
                if not sources:
                    _fail(path, lineno, "source items need expected_sources")
                if contexts:
                    _fail(path, lineno, "source items must not have reference_contexts")
                if calibration:
                    _fail(path, lineno, "calibration_sources is only allowed on fuzzy items")
            else:
                if not contexts:
                    _fail(path, lineno, "fuzzy items need reference_contexts")
                if sources:
                    _fail(path, lineno, "fuzzy items must not have expected_sources")
        elif label_type is not None or sources or contexts or calibration:
            _fail(path, lineno, "unanswerable items must have no labels")
        ground_truth = row.get("ground_truth")
        items.append(QAItem(
            id=item_id,
            query=_text(row, "query", path, lineno),
            origin=origin,
            answerable=answerable,
            label_type=label_type if answerable else None,
            expected_sources=sources,
            reference_contexts=contexts,
            calibration_sources=calibration,
            ground_truth=ground_truth if isinstance(ground_truth, str) else None,
            tags=_string_list(row, "tags", path, lineno),
        ))
    return items
