#!/usr/bin/env python3
"""Summarize archived run-evidence envelopes from the staging evidence bucket.

    python3 scripts/show_evidence.py --latest 5          # newest runs, one line each
    python3 scripts/show_evidence.py <run_id>            # one run, summarized
    python3 scripts/show_evidence.py <run_id> --json     # one run, raw envelope
    python3 scripts/show_evidence.py --latest 3 --errors # only runs that did not succeed

Reads queue-archive/<run_id>.json (the drain consumer's copy, 30-day retention). The bucket comes
from --bucket, $EVIDENCE_BUCKET, or the infra/terraform-staging output. Needs kms:Decrypt on the
evidence key. Staging runs metadata-only, so envelopes carry no prompt, answer or document text.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "queue-archive/"


def aws(args):
    result = subprocess.run(["aws", *args], text=True, capture_output=True, check=False)
    if result.returncode:
        sys.exit(f"aws {args[0]} {args[1]} failed: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'no output'}")
    return result.stdout


def default_bucket():
    if os.getenv("EVIDENCE_BUCKET"):
        return os.environ["EVIDENCE_BUCKET"]
    result = subprocess.run(
        ["terraform", f"-chdir={ROOT / 'infra/terraform-staging'}", "output", "-raw", "staging_evidence_bucket_name"],
        text=True, capture_output=True, check=False)
    if result.returncode or not result.stdout.strip():
        sys.exit("cannot determine the evidence bucket: pass --bucket or set EVIDENCE_BUCKET")
    return result.stdout.strip()


def fetch(bucket, run_id):
    return json.loads(aws(["s3", "cp", f"s3://{bucket}/{PREFIX}{run_id}.json", "-"]))


def latest_ids(bucket, count):
    listing = json.loads(aws(["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", PREFIX,
                              "--query", f"sort_by(Contents,&LastModified)[-{count}:].Key", "--output", "json"]) or "null")
    return [key.removeprefix(PREFIX).removesuffix(".json") for key in listing or []]


def summarize(envelope):
    run = envelope["safe_run"]
    ops, config = run.get("operations", {}), run.get("configuration", {})
    return {
        "run_id": envelope["run_id"], "completeness": envelope.get("completeness"),
        "status": run.get("status"), "error_class": run.get("error_class"),
        "completed_at": run.get("completed_at"), "trace_id": run.get("trace_id"),
        "latency_ms": None if ops.get("latency_ms") is None else round(ops["latency_ms"]),
        "tokens_in": ops.get("input_tokens"), "tokens_out": ops.get("output_tokens"),
        "cache_hit": ops.get("cache_hit"),
        "generation_model": config.get("generation_model"), "embedding_model": config.get("embedding_model"),
        "retrieval": [(r.get("collection"), r.get("vector_status"), r.get("graph_status"), r.get("item_count"))
                      for r in run.get("retrieval", [])],
        "operations": [e.get("name") for e in run.get("operation_events", [])],
        "unavailable": run.get("unavailable", []),
    }


def one_line(summary):
    return (f"{summary['run_id']}  {summary['status'] or '?':8} {summary['latency_ms'] or '-':>6} ms  "
            f"tok {summary['tokens_in'] or '-'}/{summary['tokens_out'] or '-'}  "
            f"cache={'hit' if summary['cache_hit'] else 'miss'}  {summary['completeness']}"
            + (f"  error={summary['error_class']}" if summary["error_class"] else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--latest", type=int, metavar="N")
    parser.add_argument("--errors", action="store_true", help="with --latest: only runs whose status is not success")
    parser.add_argument("--json", action="store_true", help="print the raw envelope for run_id")
    parser.add_argument("--bucket")
    args = parser.parse_args(argv)
    if bool(args.run_id) == bool(args.latest):
        parser.error("give either a run_id or --latest N")

    bucket = args.bucket or default_bucket()
    if args.run_id:
        envelope = fetch(bucket, args.run_id)
        if args.json:
            print(json.dumps(envelope, indent=2, sort_keys=True))
        else:
            for key, value in summarize(envelope).items():
                print(f"{key:17} {value}")
        return 0

    for run_id in latest_ids(bucket, args.latest):
        summary = summarize(fetch(bucket, run_id))
        if args.errors and summary["status"] == "success":
            continue
        print(one_line(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
