#!/usr/bin/env python3
"""
Find corpus files containing a phrase and append a source-labelled item to eval/sets/qa.jsonl.

Searches the noisy pool and true_data. A file matches when every word of --phrase appears in its
first --pages pages (PDF) or anywhere in it (other formats). Use --dry-run to list matches first.

Usage:
    python scripts/add_source_item.py --id h001 --query "How does selective state space modeling work?" \
        --phrase "selective state space" --tag mamba --dry-run
    python scripts/add_source_item.py --id h001 --query "..." --phrase "..." --pick 1
"""
import argparse
import json
import sys
from pathlib import Path

# Scripts run by path start with scripts/ on sys.path, not the repo root, so the eval package would not import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.harness.corpus import POOL_DIRS, TRUE_DIR, extract_text, list_pool  # noqa: E402


def find_matches(phrase, pages):
    words = phrase.lower().split()
    files = list_pool([*POOL_DIRS, TRUE_DIR], suffixes=(".pdf", ".docx", ".pptx", ".html", ".htm", ".txt", ".md"))
    matches = []
    for path in files:
        try:
            text = extract_text(path, max_pages=pages).lower()
        except Exception as e:
            print(f"skipped {path.name}: {type(e).__name__}", file=sys.stderr)
            continue
        if all(w in text for w in words):
            matches.append(path.name)
    return matches


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--phrase", required=True)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--pick", type=int, help="1-based index into the match list when several files match")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default="eval/sets/qa.jsonl")
    args = parser.parse_args()

    matches = find_matches(args.phrase, args.pages)
    for n, name in enumerate(matches, start=1):
        print(f"{n}. {name}")
    if args.dry_run:
        return 0
    if not matches or (len(matches) > 1 and args.pick is None):
        print(f"{len(matches)} matches; refine --phrase, raise --pages, or choose one with --pick N", file=sys.stderr)
        return 1
    chosen = matches[(args.pick or 1) - 1]

    out = Path(args.out)
    existing = {json.loads(line)["id"] for line in out.read_text().splitlines() if line.strip()} if out.exists() else set()
    if args.id in existing:
        print(f"id {args.id!r} already exists in {out}", file=sys.stderr)
        return 1
    item = {"id": args.id, "query": args.query, "origin": "hand", "answerable": True, "label_type": "source",
            "expected_sources": [chosen], "tags": args.tag}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(item) + "\n")
    print(f"added {args.id} -> {chosen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
