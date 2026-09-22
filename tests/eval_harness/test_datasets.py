import json

import pytest

from eval.harness.datasets import DatasetError, file_sha256, load_qa, load_routing


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def routing_row(**over):
    row = {"id": "r1", "query": "hi", "expected_action": "direct_answer", "expected_tool": None, "tags": ["greeting"]}
    row.update(over)
    return row


def qa_row(**over):
    row = {"id": "q1", "query": "what is x", "origin": "hand", "answerable": True,
           "label_type": "source", "expected_sources": ["a.pdf"]}
    row.update(over)
    return row


def test_load_routing_valid(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [
        routing_row(),
        routing_row(id="r2", query="3*7", expected_action="tool_use", expected_tool="calculator",
                    tags=["arithmetic"], expected_tool_output="21",
                    also_accept=[["tool_use", "sandbox"]]),
    ])
    items = load_routing(path)
    assert [i.id for i in items] == ["r1", "r2"]
    assert items[0].expected_tool is None and items[0].also_accept == ()
    assert items[1].also_accept == (("tool_use", "sandbox"),)
    assert items[1].expected_tool_output == "21"
    assert items[1].tags == ("arithmetic",)


def test_routing_duplicate_id_reports_line(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [routing_row(), routing_row()])
    with pytest.raises(DatasetError, match=r"routing.jsonl:2: duplicate id 'r1'"):
        load_routing(path)


def test_routing_bad_action(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [routing_row(expected_action="answer")])
    with pytest.raises(DatasetError, match="expected action must be one of"):
        load_routing(path)


def test_routing_tool_use_requires_known_tool(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [routing_row(expected_action="tool_use", expected_tool="nope")])
    with pytest.raises(DatasetError, match="expected tool must be one of"):
        load_routing(path)


def test_routing_non_tool_action_forbids_tool(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [routing_row(expected_tool="calculator")])
    with pytest.raises(DatasetError, match="must be null unless action is tool_use"):
        load_routing(path)


def test_routing_invalid_json_reports_line(tmp_path):
    path = tmp_path / "routing.jsonl"
    path.write_text(json.dumps(routing_row()) + "\n{not json}\n")
    with pytest.raises(DatasetError, match=r"routing.jsonl:2: invalid JSON"):
        load_routing(path)


def test_load_qa_source_fuzzy_and_unanswerable(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [
        qa_row(),
        qa_row(id="q2", origin="golden", label_type="fuzzy", expected_sources=None,
               reference_contexts=["passage"], calibration_sources=["hpa.html"], ground_truth="gt"),
        qa_row(id="q3", answerable=False, label_type=None, expected_sources=None),
    ])
    items = load_qa(path)
    assert items[0].label_type == "source" and items[0].expected_sources == ("a.pdf",)
    assert items[1].reference_contexts == ("passage",) and items[1].calibration_sources == ("hpa.html",)
    assert items[1].ground_truth == "gt"
    assert items[2].answerable is False and items[2].label_type is None


def test_qa_source_requires_sources(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(expected_sources=[])])
    with pytest.raises(DatasetError, match="source items need expected_sources"):
        load_qa(path)


def test_qa_source_rejects_reference_contexts(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(reference_contexts=["x"])])
    with pytest.raises(DatasetError, match="source items must not have reference_contexts"):
        load_qa(path)


def test_qa_calibration_sources_only_for_fuzzy(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(calibration_sources=["a.pdf"])])
    with pytest.raises(DatasetError, match="calibration_sources is only allowed on fuzzy items"):
        load_qa(path)


def test_qa_unanswerable_forbids_labels(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(answerable=False, label_type=None)])
    with pytest.raises(DatasetError, match="unanswerable items must have no labels"):
        load_qa(path)


def test_qa_bad_origin_and_non_bool_answerable(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(origin="llm")])
    with pytest.raises(DatasetError, match="origin must be one of"):
        load_qa(path)
    write_jsonl(path, [qa_row(answerable="yes")])
    with pytest.raises(DatasetError, match="answerable must be true or false"):
        load_qa(path)


def test_file_sha256_changes_with_content(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("a")
    first = file_sha256(path)
    path.write_text("b")
    assert first != file_sha256(path) and len(first) == 64
