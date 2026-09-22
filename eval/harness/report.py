import json
from pathlib import Path

from eval.harness.datasets import ACTIONS
from eval.harness.manifest import comparability
from eval.harness.stats import intervals_overlap, wilson_interval


class ComparabilityError(RuntimeError):
    pass


def save_baseline(path, manifest, summary):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"manifest": manifest, "headline": summary["headline"]}, indent=2, sort_keys=True))


def load_baseline(path):
    return json.loads(Path(path).read_text())


def check_comparable(manifest, baseline_manifest, force=False):
    differing = comparability(manifest, baseline_manifest)
    if differing and not force:
        raise ComparabilityError(
            f"baseline is not comparable; these differ: {', '.join(differing)} (--force-compare overrides)")
    return differing


def compare_headline(current, baseline):
    rows = []
    for name, cur in current.items():
        base = baseline.get(name)
        if base is None or not cur["n"] or not base["n"]:
            continue
        cur_rate, base_rate = cur["successes"] / cur["n"], base["successes"] / base["n"]
        within_noise = intervals_overlap(wilson_interval(cur["successes"], cur["n"]),
                                         wilson_interval(base["successes"], base["n"]))
        rows.append({"metric": name, "baseline": base_rate, "current": cur_rate,
                     "delta": cur_rate - base_rate, "within_noise": within_noise})
    return rows


def failure_rows(layer, items, rows, limit=25):
    by_id = {i.id: i for i in items}
    failures = []
    for r in rows:
        item = by_id[r["item_id"]]
        if layer == "routing":
            reasons = [] if r["verdict"] == "pass" else [r["verdict"]]
            if r.get("tool_check") == "fail":
                reasons.append("tool_check_failed")
            if reasons:
                failures.append({"id": item.id, "query": item.query,
                                 "expected": f"{item.expected_action}/{item.expected_tool}",
                                 "actual": f"{r['actual_action']}/{r['actual_tool']}", "reason": ", ".join(reasons)})
        elif r["verdict"] in ("unscorable", "error"):
            failures.append({"id": item.id, "query": item.query, "expected": ", ".join(item.expected_sources),
                             "actual": "", "reason": f"{r['verdict']}: {r['reason']}"})
        elif item.label_type == "source":
            names = [c["filename"] for c in r["ranked"]]
            if not any(n in item.expected_sources for n in names[:5]):
                failures.append({"id": item.id, "query": item.query, "expected": ", ".join(item.expected_sources),
                                 "actual": ", ".join(names[:3]), "reason": "miss@5"})
    return failures[:limit]


def _pct(value):
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _fmt(value):
    return "-" if value is None else f"{value:.3f}"


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    return lines + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]


def _headline_lines(headline):
    rows = []
    for name, h in headline.items():
        rate = h["successes"] / h["n"] if h["n"] else None
        lo, hi = wilson_interval(h["successes"], h["n"])
        rows.append((name, _pct(rate), f"{_pct(lo)} to {_pct(hi)}", h["n"]))
    return _table(["metric", "value", "95% CI", "n"], rows)


def _routing_sections(s):
    tag_rows = [(t, _pct(v["successes"] / v["n"] if v["n"] else None), v["n"]) for t, v in sorted(s["per_tag"].items())]
    guards = s["safeguards"]
    lines = ["", "## By tag", ""] + _table(["tag", "pass", "n"], tag_rows)
    lines += ["", "## Safeguards", "",
              f"- Accuracy gained by the safeguards (coerced minus raw): {_pct(guards['value'])}",
              f"- Fired: {guards['fired'] or 'none'}",
              f"- Simulation mismatches: {guards['mismatches']}"]
    lines += ["", "## Confusion matrix (rows expected, columns actual)", ""]
    lines += _table(["expected", *ACTIONS], [[a, *[s["confusion"][a][b] for b in ACTIONS]] for a in ACTIONS])
    te = s["tool_execution"]
    lines += ["", "## Tool execution", "", f"- pass {te['pass']}, fail {te['fail']}, skipped {te['skipped']}"]
    if s["stability"]["value"] is not None:
        unstable = s["stability"]["unstable"]
        lines += ["", "## Stability", "",
                  f"- {_pct(s['stability']['value'])} of repeated items gave the same verdict every time"
                  + (f"; unstable: {', '.join(unstable)}" if unstable else "")]
    return lines


def _retrieval_sections(s):
    metrics = ("hit@1", "hit@3", "hit@5", "mrr", "source_recall@5", "mean_top1_score")
    group_rows = [[key, g["n"], *[_fmt(g.get(m)) for m in metrics]] for key, g in s["groups"].items()]
    lines = ["", "## Groups", ""] + _table(["group", "n", *metrics], group_rows)
    fz = s["fuzzy"]
    if fz["agreement"] is None:
        status = "uncalibrated (no dual-labelled items)"
    else:
        status = f"agreement {_pct(fz['agreement'])} over {fz['n_calibration']} items, " + \
                 ("reliable" if fz["reliable"] else "UNRELIABLE (below 85%)")
    lines += ["", "## Fuzzy", "", f"- {fz['n']} fuzzy items, lexical threshold {fz['threshold']}: {status}"]
    un = s["unanswerable"]
    lines += ["", "## Unanswerable separation", "",
              f"- {un['n_answerable']} answerable vs {un['n_unanswerable']} unanswerable; AUROC {_fmt(un['auroc'])}; "
              f"suggested cutoff {_fmt(un['cutoff'])}"
              + ("; descriptive only (fewer than 20 unanswerable items)" if un["descriptive_only"] else "")]
    lines += ["", "## Arms", "",
              f"- vector documents {s['arms']['vector_docs']}, graph documents {s['arms']['graph_docs']}",
              f"- empty-context rate {_pct(s['empty_context_rate'])}"]
    return lines


def render_summary_md(layer, manifest, summary, warnings, failures, comparison=None):
    lines = [f"# Eval run: {layer}", ""]
    if summary.get("invalid"):
        lines += ["**INVALID RUN**: the planner's LLM call failed too often, so this measured an outage, not routing.", ""]
    lines += ["## Manifest", "",
              f"- started {manifest['started_at']}; model {manifest['model']} at {manifest['endpoint_host']}",
              f"- embeddings {manifest['embed_model']} ({manifest['embed_dim']}-dim)",
              f"- git {manifest['git_sha'][:10]}{' (dirty)' if manifest['git_dirty'] else ''}",
              f"- datasets {', '.join(f'{k}@{v[:8]}' for k, v in manifest['dataset_hashes'].items())}"
              + (f"; corpus {manifest['corpus_hash'][:8]}" if manifest.get("corpus_hash") else ""),
              f"- Qdrant points {manifest.get('qdrant_points')}; Neo4j nodes {manifest.get('neo4j_nodes')}"]
    if warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in warnings]
    lines += ["", "## Headline", ""] + _headline_lines(summary["headline"])
    lines += _routing_sections(summary) if layer == "routing" else _retrieval_sections(summary)
    lat = summary["latency_ms"]
    lines += ["", "## Latency", "", f"- p50 {_fmt(lat['p50'])} ms, p95 {_fmt(lat['p95'])} ms"]
    if comparison:
        lines += ["", "## Comparison with baseline", ""]
        lines += _table(["metric", "baseline", "current", "delta", "verdict"],
                        [(c["metric"], _pct(c["baseline"]), _pct(c["current"]), f"{100 * c['delta']:+.1f} pts",
                          "within noise" if c["within_noise"] else "outside noise") for c in comparison])
    if failures:
        lines += ["", "## Failures", ""]
        lines += _table(["id", "query", "expected", "actual", "reason"],
                        [(f["id"], f["query"][:60], f["expected"], f["actual"], f["reason"]) for f in failures])
    return "\n".join(lines) + "\n"


def write_run(out_dir, manifest, rows, summary, markdown):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    (out / "results.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    (out / "summary.md").write_text(markdown)
