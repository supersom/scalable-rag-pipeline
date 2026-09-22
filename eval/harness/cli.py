import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from eval.harness import probes
from eval.harness.datasets import DatasetError, file_sha256, load_qa, load_routing
from eval.harness.manifest import build_manifest, git_info, planner_prompt_hash
from eval.harness.preflight import run_preflight
from eval.harness.recorders import PlannerRecorder, RecorderError, RetrievalRecorder
from eval.harness.report import (
    ComparabilityError, check_comparable, compare_headline, failure_rows, load_baseline, render_summary_md,
    save_baseline, write_run,
)
from eval.harness.retrieval import calibrate_threshold, run_retrieval, summarize_retrieval
from eval.harness.routing import FALLBACK_PLAN_PREFIX, run_routing, summarize_routing

EXIT_OK = 0
EXIT_INVALID = 2


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m eval.harness")
    layers = parser.add_subparsers(dest="layer", required=True)
    routing = layers.add_parser("routing", help="score planner routing")
    routing.add_argument("--dataset", default="eval/sets/routing.jsonl")
    routing.add_argument("--repeats", type=int, default=1)
    retrieval = layers.add_parser("retrieval", help="score retrieval")
    retrieval.add_argument("--dataset", default="eval/sets/qa.jsonl")
    retrieval.add_argument("--corpus-manifest", default="eval/sets/corpus_manifest.json")
    for sub in (routing, retrieval):
        sub.add_argument("--out", default=None)
        sub.add_argument("--set-baseline", action="store_true")
        sub.add_argument("--compare", action="store_true")
        sub.add_argument("--force-compare", action="store_true")
    return parser


def exit_code(invalid, preflight_ok, comparison_error):
    return EXIT_INVALID if invalid or not preflight_ok or comparison_error else EXIT_OK


def verify_planner_contract(module):
    if FALLBACK_PLAN_PREFIX not in Path(module.__file__).read_text():
        raise RecorderError(
            f"planner module no longer contains {FALLBACK_PLAN_PREFIX!r}; update FALLBACK_PLAN_PREFIX in eval/harness/routing.py")


async def _run(args):
    from dotenv import load_dotenv

    load_dotenv()

    from services.api.app.agents.nodes import planner as planner_module
    from services.api.app.agents.nodes.retriever import retrieve_node
    from services.api.app.agents.nodes.tool import tool_node
    from services.api.app.clients.qdrant import qdrant_client
    from services.api.app.clients.ray_llm import llm_client
    from services.api.app.config import settings
    from services.api.app.tools.registry import TOOL_REGISTRY

    layer = args.layer
    out_dir = Path(args.out) if args.out else Path("eval/runs") / f"{datetime.now():%Y%m%d-%H%M%S}-{layer}"
    dataset_path = Path(args.dataset)
    corpus_path = None
    if layer == "routing":
        items, expected_sources = load_routing(dataset_path), set()
    else:
        items = load_qa(dataset_path)
        expected_sources = {s for i in items if i.label_type == "source" for s in i.expected_sources}
        corpus_path = Path(args.corpus_manifest)

    await llm_client.start()
    try:
        pre = await run_preflight(
            layer=layer, embed_dim=settings.EMBED_DIM, expected_sources=expected_sources,
            probes={"llm": probes.llm_probe, "embed": probes.embed_probe,
                    "qdrant": probes.qdrant_probe, "neo4j": probes.neo4j_probe})
        for message in pre.errors:
            print(f"ERROR: {message}", file=sys.stderr)
        for message in pre.warnings:
            print(f"warning: {message}", file=sys.stderr)
        if not pre.ok:
            return EXIT_INVALID
        if layer == "routing":
            verify_planner_contract(planner_module)
            recorder = PlannerRecorder(planner_module)
            with recorder:
                results = await run_routing(items, args.repeats, planner_module.planner_node, tool_node,
                                            recorder, set(TOOL_REGISTRY))
            summary = summarize_routing(items, results)
        else:
            if pre.facts.get("neo4j_nodes") is None:
                logging.getLogger("services.api.app.agents.nodes.retriever").setLevel(logging.CRITICAL)
            recorder = RetrievalRecorder(qdrant_client)
            with recorder:
                results = await run_retrieval(items, retrieve_node, recorder, pre.facts["filenames"])
            threshold, agreement, n_dual = calibrate_threshold(items, results)
            summary = summarize_retrieval(items, results, threshold, agreement, n_dual)
    finally:
        await llm_client.close()

    sha, dirty = git_info()
    corpus_hash = corpus_seed = None
    if corpus_path is not None and corpus_path.exists():
        corpus_hash = file_sha256(corpus_path)
        corpus_seed = json.loads(corpus_path.read_text()).get("seed")
    manifest = build_manifest(
        layer=layer, model=settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME, endpoint=settings.RAY_LLM_ENDPOINT,
        embed_model=settings.EMBED_MODEL_NAME, embed_dim=settings.EMBED_DIM,
        prompt_hash=planner_prompt_hash(planner_module.SYSTEM_PROMPT), git_sha=sha, git_dirty=dirty,
        dataset_hashes={dataset_path.name: file_sha256(dataset_path)}, corpus_hash=corpus_hash,
        corpus_seed=corpus_seed, qdrant_points=pre.facts.get("qdrant_points"),
        qdrant_vector_size=pre.facts.get("qdrant_vector_size"), neo4j_nodes=pre.facts.get("neo4j_nodes"))

    baseline_path = Path("eval/baselines") / f"{layer}.json"
    comparison, comparison_error = None, None
    if args.compare or args.force_compare:
        if not baseline_path.exists():
            pre.warnings.append(f"No baseline at {baseline_path}; run once with --set-baseline first.")
        else:
            baseline = load_baseline(baseline_path)
            try:
                differing = check_comparable(manifest, baseline["manifest"], force=args.force_compare)
                if differing:
                    pre.warnings.append(f"Compared across differing {', '.join(differing)} (forced).")
                comparison = compare_headline(summary["headline"], baseline["headline"])
            except ComparabilityError as e:
                comparison_error = str(e)
                pre.warnings.append(comparison_error)

    rows = [asdict(r) for r in results]
    markdown = render_summary_md(layer, manifest, summary, pre.warnings, failure_rows(layer, items, rows), comparison)
    write_run(out_dir, manifest, rows, summary, markdown)
    if args.set_baseline and not summary["invalid"]:
        save_baseline(baseline_path, manifest, summary)
        print(f"baseline saved: {baseline_path}")
    print(f"run written to {out_dir}")
    for name, h in summary["headline"].items():
        print(f"  {name}: {h['successes']}/{h['n']}")
    return exit_code(summary["invalid"], True, comparison_error)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not Path("services/api/app").is_dir():
        print("error: run from the repo root, e.g. python -m eval.harness routing", file=sys.stderr)
        return EXIT_INVALID
    try:
        return asyncio.run(_run(args))
    except (DatasetError, RecorderError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INVALID
