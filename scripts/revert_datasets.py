#!/usr/bin/env python3
"""
Move files from eval/datasets/{ingested,digested} back to eval/datasets/noisy_data.

Dry run by default; pass --apply to move. Never overwrites: a file whose name
already exists in noisy_data is skipped and reported.

Does not touch Qdrant or Neo4j. Re-ingesting files that are still indexed
creates duplicate points.

Usage:
    python scripts/revert_datasets.py           # preview
    python scripts/revert_datasets.py --apply   # move
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOISY_DIR = ROOT / "eval/datasets/noisy_data"
SOURCE_DIRS = [ROOT / "eval/datasets/ingested", ROOT / "eval/datasets/digested"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    args = parser.parse_args()

    NOISY_DIR.mkdir(parents=True, exist_ok=True)
    moved = {d.name: 0 for d in SOURCE_DIRS}
    skipped = []

    for src_dir in SOURCE_DIRS:
        for f in sorted(p for p in src_dir.iterdir() if p.is_file()):
            dest = NOISY_DIR / f.name
            if dest.exists():
                skipped.append((src_dir.name, f.name))
                continue
            if args.apply:
                f.rename(dest)
            moved[src_dir.name] += 1

    verb = "moved" if args.apply else "would move"
    for name, n in moved.items():
        print(f"{verb} {n:>4} files: {name}/ -> noisy_data/")
    for src_name, fname in skipped:
        print(f"SKIPPED (already in noisy_data): {src_name}/{fname}")
    if not args.apply:
        print("dry run only; re-run with --apply to move")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
