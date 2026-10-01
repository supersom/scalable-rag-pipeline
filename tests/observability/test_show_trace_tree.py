"""The trace tree viewer reads the collector's debug-exporter text."""

from scripts.show_trace_tree import main, parse_spans, render, group_traces

LOG = """\
Span #0
    Trace ID       : aaaa
    Parent ID      :
    ID             : root
    Name           : agent.run
    Start time     : 2026-10-01 01:00:00.000000000 +0000 UTC
    End time       : 2026-10-01 01:00:02.000000000 +0000 UTC
Attributes:
     -> run_id: Str(11111111-1111-1111-1111-111111111111)
Span #1
    Trace ID       : aaaa
    Parent ID      : root
    ID             : plan
    Name           : planner
    Start time     : 2026-10-01 01:00:00.500000000 +0000 UTC
    End time       : 2026-10-01 01:00:01.000000000 +0000 UTC
Attributes:
     -> operation.status: Str(error)
Span #2
    Trace ID       : aaaa
    Parent ID      : root
    ID             : chunk
    Name           : POST /x http send
    Start time     : 2026-10-01 01:00:01.000000000 +0000 UTC
    End time       : 2026-10-01 01:00:01.100000000 +0000 UTC
Span #3
    Trace ID       : bbbb
    Parent ID      :
    ID             : other
    Name           : GET /health
    Start time     : 2026-10-01 01:00:03.000000000 +0000 UTC
    End time       : 2026-10-01 01:00:03.001000000 +0000 UTC
"""


def run(args, text=LOG):
    import io
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(args, stdin=text.splitlines(True))
    return code, out.getvalue()


def test_tree_nests_children_shows_durations_and_hides_chunk_spans():
    traces = group_traces(parse_spans(LOG.splitlines(True)))
    assert render(traces["aaaa"]) == ["- agent.run  2000 ms", "  - planner  500 ms  [error]"]
    assert any("http send" in line for line in render(traces["aaaa"], show_noise=True))


def test_select_by_run_id_and_list_only_request_traces():
    code, out = run(["--run-id", "11111111-1111-1111-1111-111111111111"])
    assert code == 0 and "trace_id=aaaa" in out
    code, out = run(["--list"])
    assert "1 request traces (2 total)" in out


def test_compose_prefixed_lines_and_missing_trace():
    prefixed = "".join("otel-collector-1  | " + line for line in LOG.splitlines(True))
    assert run(["--list"], prefixed)[1].count("run_id=") == 1
    assert run(["--run-id", "nope"])[0] == 1
