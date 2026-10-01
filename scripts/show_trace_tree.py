#!/usr/bin/env python3
"""Print request traces as span trees from OpenTelemetry Collector `debug` exporter output.

Staging and local Compose export traces only to the collector's `debug` exporter, so its log is
the trace store. Pipe it in:

    kubectl logs deployment/otel-collector -n staging-evidence --tail=20000 | python3 scripts/show_trace_tree.py
    ... | python3 scripts/show_trace_tree.py --run-id <x-run-id header value>
    ... | python3 scripts/show_trace_tree.py --list
    docker compose logs otel-collector | python3 scripts/show_trace_tree.py
"""

import argparse
import re
import sys
from datetime import datetime, timezone

SPAN_FIELDS = (("Trace ID", "trace_id"), ("Parent ID", "parent_id"), ("ID", "span_id"),
               ("Name", "name"), ("Start time", "start"), ("End time", "end"))
NOISE_SUFFIXES = (" http send", " http receive")
COMPOSE_PREFIX = re.compile(r"^\S+\s+\|\s?")
TIME = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.(\d+))? \+0000 UTC")


def parse_time(text):
    match = TIME.search(text or "")
    if match is None:
        return None
    day, clock, fraction = match.groups()
    micros = (fraction or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{day}T{clock}.{micros}").replace(tzinfo=timezone.utc)


def parse_spans(lines):
    spans, current = [], None
    for line in lines:
        line = COMPOSE_PREFIX.sub("", line.rstrip("\n"))
        if re.match(r"^Span #\d+\s*$", line):
            current = {"attrs": {}}
            spans.append(current)
            continue
        if current is None:
            continue
        attr = re.match(r"^\s+-> ([\w.]+): \w+\((.*)\)\s*$", line)
        if attr:
            current["attrs"][attr.group(1)] = attr.group(2)
            continue
        for label, field in SPAN_FIELDS:
            match = re.match(rf"^\s+{label}\s*:\s*(.*)$", line)
            if match and field not in current:
                current[field] = match.group(1).strip()
                break
    return [s for s in spans if s.get("trace_id") and s.get("span_id")]


def group_traces(spans):
    traces = {}
    for span in spans:
        traces.setdefault(span["trace_id"], []).append(span)
    return traces


def run_id_of(trace):
    return next((s["attrs"]["run_id"] for s in trace if s.get("name") == "agent.run" and "run_id" in s["attrs"]), None)


def duration_ms(span):
    start, end = parse_time(span.get("start")), parse_time(span.get("end"))
    return None if start is None or end is None else (end - start).total_seconds() * 1000


def render(trace, *, show_noise=False):
    by_id = {s["span_id"]: s for s in trace}
    children = {}
    for span in trace:
        if not show_noise and span.get("name", "").endswith(NOISE_SUFFIXES):
            continue
        parent = span.get("parent_id") if span.get("parent_id") in by_id else None
        children.setdefault(parent, []).append(span)
    for siblings in children.values():
        siblings.sort(key=lambda s: parse_time(s.get("start")) or datetime.min.replace(tzinfo=timezone.utc))
    lines = []

    def walk(parent, depth):
        for span in children.get(parent, []):
            took = duration_ms(span)
            suffix = "" if took is None else f"  {took:.0f} ms"
            status = span["attrs"].get("operation.status")
            if status and status != "success":
                suffix += f"  [{status}]"
            lines.append("  " * depth + "- " + span.get("name", "?") + suffix)
            walk(span["span_id"], depth + 1)

    walk(None, 0)
    return lines


def main(argv=None, stdin=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pick = parser.add_mutually_exclusive_group()
    pick.add_argument("--run-id", help="the x-run-id response header, or run_id in the evidence envelope")
    pick.add_argument("--trace-id", help="a trace_id, for example from the evidence envelope")
    pick.add_argument("--list", action="store_true", help="list traces instead of drawing one")
    parser.add_argument("--all", action="store_true", help="include per-chunk http send/receive spans")
    args = parser.parse_args(argv)

    traces = group_traces(parse_spans(stdin if stdin is not None else sys.stdin))
    request_traces = {tid: t for tid, t in traces.items() if run_id_of(t)}
    if args.list:
        for tid, trace in request_traces.items():
            print(f"{tid}  run_id={run_id_of(trace)}  spans={len(trace)}")
        print(f"{len(request_traces)} request traces ({len(traces)} total) in this log window")
        return 0

    if args.run_id:
        chosen = next((tid for tid, t in request_traces.items() if run_id_of(t) == args.run_id), None)
    elif args.trace_id:
        chosen = args.trace_id if args.trace_id in traces else None
    else:
        chosen = next(reversed(request_traces), None)
    if chosen is None:
        print("no matching trace in this log window (the collector log is a bounded buffer; "
              "raise --tail or check that the collector has not restarted)", file=sys.stderr)
        return 1
    print(f"trace_id={chosen}  run_id={run_id_of(traces[chosen])}  spans={len(traces[chosen])}")
    print("\n".join(render(traces[chosen], show_noise=args.all)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
