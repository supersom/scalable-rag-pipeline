import asyncio

from eval.harness.datasets import RoutingItem
from eval.harness.routing import (
    RoutingResult, judge, raw_verdict, run_item, run_routing, simulate_planner, summarize_routing,
)

TOOLS = {"calculator", "web_search", "sandbox", "graph_search"}


def item(id="a", action="direct_answer", tool=None, tags=(), also=(), output=None, query="q"):
    return RoutingItem(id=id, query=query, expected_action=action, expected_tool=tool, tags=tuple(tags),
                       also_accept=tuple(also), expected_tool_output=output)


class FakeRecorder:
    def __init__(self):
        self.value = None

    def reset(self):
        self.value = None

    def last(self):
        return self.value


def planner(recorder, raw, out):
    async def fn(state):
        recorder.value = raw
        return out
    return fn


def tool(text):
    async def fn(state):
        return {"documents": [text]}
    return fn


def out(action, tool_name="", tool_input="", plan=None):
    return {"action": action, "tool_name": tool_name, "current_query": "q", "tool_input": tool_input,
            "plan": plan or [f"action={action}"]}


CALC = item("c", "tool_use", "calculator", output="21")
FALLBACK = "Error in planning, defaulting to retrieval."


def test_judge():
    assert judge(CALC, "tool_use", "calculator") == "pass"
    assert judge(CALC, "tool_use", "web_search") == "wrong_tool"
    assert judge(CALC, "retrieve", None) == "wrong_action"
    ambiguous = item(action="tool_use", tool="web_search", also=[("tool_use", "calculator")])
    assert judge(ambiguous, "tool_use", "calculator") == "pass"
    assert judge(item(), "direct_answer", None) == "pass"


def test_raw_verdict():
    assert raw_verdict(CALC, None) == "invalid"
    assert raw_verdict(CALC, {"_error": "x"}) == "invalid"
    assert raw_verdict(CALC, {"action": "answer"}) == "wrong_action"
    assert raw_verdict(CALC, {"action": "tool_use", "tool_name": "calculator"}) == "pass"
    assert raw_verdict(CALC, {"action": "retrieve", "tool_name": "calculator"}) == "wrong_action"
    assert raw_verdict(item(action="retrieve"), {"action": "retrieve", "tool_name": "calculator"}) == "wrong_tool"


def test_simulate_planner_covers_each_safeguard():
    assert simulate_planner({"_error": "x"}, TOOLS) == (["raw_invalid_json"], "retrieve", None)
    assert simulate_planner({"action": "tool_use", "tool_name": "nope"}, TOOLS) == (["hallucinated_tool"], "retrieve", None)
    assert simulate_planner({"action": "retrieve", "tool_name": "calculator"}, TOOLS) == (["tool_promoted"], "tool_use", "calculator")
    assert simulate_planner({"action": "bogus", "tool_name": "calculator"}, TOOLS) == (["invalid_action"], "retrieve", None)
    assert simulate_planner({"action": "direct_answer", "tool_name": "nope"}, TOOLS) == (["direct_answer_bad_tool"], "retrieve", None)
    assert simulate_planner({"action": "tool_use", "tool_name": ["calculator"]}, TOOLS) == (["tool_name_list_unwrapped"], "tool_use", "calculator")
    assert simulate_planner({"action": "retrieve", "tool_name": None}, TOOLS) == ([], "retrieve", None)


def run(it, planner_fn, tool_fn=None, recorder=None):
    recorder = recorder or FakeRecorder()
    return asyncio.run(run_item(it, 0, planner_fn, tool_fn or tool(""), recorder, TOOLS))


def test_run_item_pass_with_tool_check():
    rec = FakeRecorder()
    raw = {"action": "tool_use", "tool_name": "calculator"}
    result = run(CALC, planner(rec, raw, out("tool_use", "calculator", "3*7")), tool("21"), rec)
    assert result.verdict == "pass" and result.raw_verdict == "pass"
    assert result.tool_check == "pass" and result.tool_output == "21"
    assert result.safeguards == [] and result.safeguard_mismatch is False
    assert result.latency_ms is not None


def test_run_item_tool_check_fails_when_calculator_errors():
    rec = FakeRecorder()
    raw = {"action": "tool_use", "tool_name": "calculator"}
    result = run(item("s", "tool_use", "calculator", output="16"),
                 planner(rec, raw, out("tool_use", "calculator", "sqrt(256)")),
                 tool("Error: Function 'sqrt' not defined"), rec)
    assert result.verdict == "pass" and result.tool_check == "fail"


def test_run_item_tool_check_skipped_when_not_routed_to_calculator():
    rec = FakeRecorder()
    raw = {"action": "tool_use", "tool_name": "web_search"}
    result = run(CALC, planner(rec, raw, out("tool_use", "web_search", "q")), tool("x"), rec)
    assert result.verdict == "wrong_tool"
    assert result.tool_check.startswith("skipped")


def test_run_item_planner_failure_is_not_scored():
    rec = FakeRecorder()
    result = run(item(action="retrieve"), planner(rec, None, out("retrieve", plan=[FALLBACK])), recorder=rec)
    assert result.verdict == "planner_failed"


def test_run_item_invalid_json_is_a_model_failure():
    rec = FakeRecorder()
    result = run(item(action="direct_answer"),
                 planner(rec, {"_error": "No JSON object found"}, out("retrieve", plan=[FALLBACK])), recorder=rec)
    assert result.verdict == "wrong_action" and result.raw_verdict == "invalid"
    assert result.safeguards == ["raw_invalid_json"] and result.safeguard_mismatch is False


def test_run_item_flags_safeguard_simulation_drift():
    rec = FakeRecorder()
    result = run(CALC, planner(rec, {"action": "retrieve", "tool_name": None}, out("tool_use", "calculator")), recorder=rec)
    assert result.safeguard_mismatch is True


def test_run_item_records_exceptions():
    async def boom(state):
        raise RuntimeError("down")

    result = run(item(), boom)
    assert result.verdict == "error" and "RuntimeError: down" in result.error


def test_run_routing_repeats_each_item():
    rec = FakeRecorder()
    items = [item("a"), item("b")]
    results = asyncio.run(run_routing(items, 2, planner(rec, {"action": "direct_answer", "tool_name": None},
                                                       out("direct_answer")), tool(""), rec, TOOLS))
    assert [(r.item_id, r.repeat) for r in results] == [("a", 0), ("a", 1), ("b", 0), ("b", 1)]


def result(item_id, verdict, action, tool_name=None, raw=None, fired=(), check=None, repeat=0, latency=10.0):
    return RoutingResult(item_id=item_id, repeat=repeat, verdict=verdict, actual_action=action, actual_tool=tool_name,
                         raw_verdict=raw, safeguards=list(fired), tool_check=check, latency_ms=latency)


def test_summarize_routing_headline_tags_confusion_and_safeguards():
    items = [
        item("a", tags=["greeting"]),
        item("b", "tool_use", "calculator", tags=["arithmetic"], output="1"),
        item("c", "retrieve", tags=["factual"]),
        item("e", "retrieve", tags=["typo"]),
        item("d", "retrieve", tags=["coreference", "known-limitation"]),
    ]
    results = [
        result("a", "pass", "direct_answer", raw="pass"),
        result("b", "wrong_tool", "tool_use", "web_search", raw="wrong_tool", check="skipped: routed elsewhere"),
        result("c", "wrong_action", "direct_answer", raw="wrong_action"),
        result("e", "pass", "retrieve", raw="wrong_action", fired=["invalid_action"]),
        result("d", "wrong_action", "direct_answer", raw="wrong_action"),
        result("f", "planner_failed", "retrieve"),
    ]
    s = summarize_routing(items, results)
    assert s["headline"]["pass_rate"] == {"successes": 2, "n": 4}
    assert s["headline"]["action_accuracy"] == {"successes": 3, "n": 4}
    assert s["headline"]["tool_accuracy"] == {"successes": 0, "n": 1}
    assert s["per_tag"]["greeting"] == {"successes": 1, "n": 1}
    assert s["per_tag"]["known-limitation"] == {"successes": 0, "n": 1}
    assert s["confusion"]["retrieve"]["direct_answer"] == 2 and s["confusion"]["retrieve"]["retrieve"] == 1
    assert s["confusion"]["direct_answer"]["direct_answer"] == 1 and s["confusion"]["tool_use"]["tool_use"] == 1
    assert s["per_tool"]["calculator"] == {"precision": None, "recall": 0.0}
    assert s["per_tool"]["web_search"] == {"precision": 0.0, "recall": None}
    assert s["safeguards"] == {"fired": {"invalid_action": 1}, "value": 0.25, "mismatches": 0}
    assert s["tool_execution"] == {"pass": 0, "fail": 0, "skipped": 1}
    assert s["planner_failed"]["count"] == 1 and s["invalid"] is False
    assert s["stability"] == {"value": None, "unstable": []}
    assert s["latency_ms"] == {"p50": 10.0, "p95": 10.0}


def test_summarize_routing_marks_run_invalid_when_planner_mostly_failed():
    items = [item("a"), item("b")]
    results = [result("a", "planner_failed", "retrieve"), result("b", "pass", "direct_answer", raw="pass")]
    s = summarize_routing(items, results)
    assert s["planner_failed"]["rate"] == 0.5 and s["invalid"] is True


def test_summarize_routing_reports_unstable_items():
    items = [item("a"), item("b")]
    results = [
        result("a", "pass", "direct_answer", raw="pass", repeat=0),
        result("a", "wrong_action", "retrieve", raw="wrong_action", repeat=1),
        result("b", "pass", "direct_answer", raw="pass", repeat=0),
        result("b", "pass", "direct_answer", raw="pass", repeat=1),
    ]
    s = summarize_routing(items, results)
    assert s["stability"] == {"value": 0.5, "unstable": ["a"]}
