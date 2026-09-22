import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from eval.harness.datasets import ACTIONS, TOOLS
from eval.harness.stats import percentile

# Prefix of the plan text planner_node emits from its blanket except; the CLI verifies it still exists in the source.
FALLBACK_PLAN_PREFIX = "Error in planning"
INVALID_RUN_PLANNER_FAILED_RATE = 0.20
KNOWN_LIMITATION_TAG = "known-limitation"
SCORED = ("pass", "wrong_action", "wrong_tool")


@dataclass
class RoutingResult:
    item_id: str
    repeat: int
    verdict: str
    actual_action: Optional[str] = None
    actual_tool: Optional[str] = None
    raw_plan: Optional[dict] = None
    raw_verdict: Optional[str] = None
    safeguards: list = field(default_factory=list)
    safeguard_mismatch: bool = False
    tool_check: Optional[str] = None
    tool_output: Optional[str] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None


def _tool_of(value):
    if isinstance(value, list):
        value = value[0] if value else ""
    return value or None


def judge(item, action, tool):
    acceptable = {(item.expected_action, item.expected_tool), *item.also_accept}
    if (action, tool) in acceptable:
        return "pass"
    if action in {a for a, _ in acceptable}:
        return "wrong_tool"
    return "wrong_action"


def raw_verdict(item, raw_plan):
    if raw_plan is None or "_error" in raw_plan:
        return "invalid"
    action = raw_plan.get("action", "retrieve")
    if action not in ACTIONS:
        return "wrong_action"
    return judge(item, action, _tool_of(raw_plan.get("tool_name")))


def simulate_planner(raw_plan, known_tools):
    if "_error" in raw_plan:
        return ["raw_invalid_json"], "retrieve", None
    fired = []
    action = raw_plan.get("action", "retrieve")
    tool = raw_plan.get("tool_name") or ""
    if isinstance(tool, list):
        fired.append("tool_name_list_unwrapped")
        tool = tool[0] if tool else ""
    if action not in ACTIONS:
        fired.append("invalid_action")
        action, tool = "retrieve", ""
    if tool and tool in known_tools and action != "tool_use":
        fired.append("tool_promoted")
        action = "tool_use"
    if action == "tool_use" and tool not in known_tools:
        fired.append("hallucinated_tool")
        action, tool = "retrieve", ""
    if action == "direct_answer" and tool and tool not in known_tools:
        fired.append("direct_answer_bad_tool")
        action, tool = "retrieve", ""
    return fired, action, tool or None


async def _check_tool(item, action, tool, planned, tool_fn):
    if action != "tool_use" or tool != "calculator":
        return f"skipped: routed to {action}/{tool}, only calculator is executed", None
    try:
        executed = await tool_fn({"tool_name": tool, "tool_input": planned.get("tool_input", "")})
        output = executed["documents"][0]
        return ("pass" if re.search(item.expected_tool_output, output) else "fail"), output
    except Exception as e:
        return "fail", f"{type(e).__name__}: {e}"


async def run_item(item, repeat, planner_fn, tool_fn, recorder, known_tools):
    recorder.reset()
    state = {"messages": [{"role": "user", "content": item.query}], "current_query": item.query,
             "action": "retrieve", "tool_name": "", "tool_input": "", "documents": [], "plan": []}
    started = time.perf_counter()
    try:
        planned = await planner_fn(state)
    except Exception as e:
        return RoutingResult(item.id, repeat, "error", error=f"{type(e).__name__}: {e}",
                             latency_ms=(time.perf_counter() - started) * 1000)
    latency_ms = (time.perf_counter() - started) * 1000
    raw = recorder.last()
    action = planned.get("action")
    tool = planned.get("tool_name") or None
    if " ".join(planned.get("plan") or []).startswith(FALLBACK_PLAN_PREFIX) and raw is None:
        return RoutingResult(item.id, repeat, "planner_failed", action, tool, latency_ms=latency_ms)
    fired, sim_action, sim_tool = simulate_planner(raw, known_tools) if raw is not None else ([], action, tool)
    tool_check = tool_output = None
    if item.expected_tool_output is not None:
        tool_check, tool_output = await _check_tool(item, action, tool, planned, tool_fn)
    return RoutingResult(
        item_id=item.id, repeat=repeat, verdict=judge(item, action, tool), actual_action=action, actual_tool=tool,
        raw_plan=raw, raw_verdict=raw_verdict(item, raw) if raw is not None else None, safeguards=fired,
        safeguard_mismatch=(sim_action, sim_tool) != (action, tool), tool_check=tool_check,
        tool_output=tool_output, latency_ms=latency_ms,
    )


async def run_routing(items, repeats, planner_fn, tool_fn, recorder, known_tools):
    results = []
    for item in items:
        for repeat in range(repeats):
            results.append(await run_item(item, repeat, planner_fn, tool_fn, recorder, known_tools))
    return results


def _rate(successes, n):
    return {"successes": successes, "n": n}


def summarize_routing(items, results):
    by_id = {i.id: i for i in items}
    scored = [r for r in results if r.verdict in SCORED]
    rows = [r for r in scored if KNOWN_LIMITATION_TAG not in by_id[r.item_id].tags]

    def action_ok(r):
        return r.verdict in ("pass", "wrong_tool")

    headline = {
        "pass_rate": _rate(sum(r.verdict == "pass" for r in rows), len(rows)),
        "action_accuracy": _rate(sum(action_ok(r) for r in rows), len(rows)),
    }
    tool_rows = [r for r in rows if by_id[r.item_id].expected_action == "tool_use" and action_ok(r)]
    if tool_rows:
        headline["tool_accuracy"] = _rate(sum(r.verdict == "pass" for r in tool_rows), len(tool_rows))

    per_tag = defaultdict(lambda: {"successes": 0, "n": 0})
    for r in scored:
        for tag in by_id[r.item_id].tags:
            per_tag[tag]["n"] += 1
            per_tag[tag]["successes"] += r.verdict == "pass"

    confusion = {a: {b: 0 for b in ACTIONS} for a in ACTIONS}
    for r in scored:
        if r.actual_action in ACTIONS:
            confusion[by_id[r.item_id].expected_action][r.actual_action] += 1

    per_tool = {}
    for name in TOOLS:
        hits = sum(1 for r in scored if by_id[r.item_id].expected_tool == name and r.actual_tool == name)
        predicted = sum(1 for r in scored if r.actual_tool == name)
        expected = sum(1 for r in scored if by_id[r.item_id].expected_tool == name)
        per_tool[name] = {"precision": hits / predicted if predicted else None,
                          "recall": hits / expected if expected else None}

    with_raw = [r for r in rows if r.raw_verdict is not None]
    value = None
    if with_raw:
        value = (sum(r.verdict == "pass" for r in with_raw) - sum(r.raw_verdict == "pass" for r in with_raw)) / len(with_raw)

    verdicts = defaultdict(list)
    for r in results:
        verdicts[r.item_id].append(r.verdict)
    repeated = {k: v for k, v in verdicts.items() if len(v) > 1}
    unstable = sorted(k for k, v in repeated.items() if len(set(v)) > 1)
    stability = 1 - len(unstable) / len(repeated) if repeated else None

    tool_execution = {"pass": 0, "fail": 0, "skipped": 0}
    for r in results:
        if r.tool_check is not None:
            tool_execution["skipped" if r.tool_check.startswith("skipped") else r.tool_check] += 1

    failed = sum(r.verdict == "planner_failed" for r in results)
    failed_rate = failed / len(results) if results else 0.0
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    return {
        "headline": headline,
        "per_tag": dict(per_tag),
        "confusion": confusion,
        "per_tool": per_tool,
        "safeguards": {"fired": dict(Counter(n for r in results for n in r.safeguards)), "value": value,
                       "mismatches": sum(r.safeguard_mismatch for r in results)},
        "stability": {"value": stability, "unstable": unstable},
        "tool_execution": tool_execution,
        "planner_failed": {"count": failed, "rate": failed_rate},
        "errors": sum(r.verdict == "error" for r in results),
        "invalid": failed_rate > INVALID_RUN_PLANNER_FAILED_RATE,
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "n_items": len(items),
        "n_results": len(results),
    }
