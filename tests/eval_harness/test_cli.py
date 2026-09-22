import types

import pytest

from eval.harness.cli import build_parser, exit_code, main, verify_planner_contract
from eval.harness.recorders import RecorderError


def test_parser_defaults():
    routing = build_parser().parse_args(["routing"])
    assert routing.layer == "routing" and routing.repeats == 1 and routing.dataset == "eval/sets/routing.jsonl"
    assert routing.out is None and not routing.set_baseline and not routing.compare and not routing.force_compare
    retrieval = build_parser().parse_args(["retrieval"])
    assert retrieval.dataset == "eval/sets/qa.jsonl" and retrieval.corpus_manifest == "eval/sets/corpus_manifest.json"


def test_parser_flags():
    args = build_parser().parse_args(["routing", "--repeats", "3", "--set-baseline", "--compare", "--out", "x"])
    assert args.repeats == 3 and args.set_baseline and args.compare and args.out == "x"
    assert build_parser().parse_args(["retrieval", "--force-compare"]).force_compare


def test_parser_requires_a_layer():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_exit_code():
    assert exit_code(False, True, None) == 0
    assert exit_code(True, True, None) == 2
    assert exit_code(False, False, None) == 2
    assert exit_code(False, True, "not comparable") == 2


def test_verify_planner_contract(tmp_path):
    good = tmp_path / "planner_ok.py"
    good.write_text('plan = ["Error in planning, defaulting to retrieval."]\n')
    verify_planner_contract(types.SimpleNamespace(__file__=str(good)))
    bad = tmp_path / "planner_changed.py"
    bad.write_text('plan = ["Planner failed"]\n')
    with pytest.raises(RecorderError, match="FALLBACK_PLAN_PREFIX"):
        verify_planner_contract(types.SimpleNamespace(__file__=str(bad)))


def test_main_refuses_to_run_outside_the_repo_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["routing"]) == 2
    assert "repo root" in capsys.readouterr().err
