#!/usr/bin/env python3
"""
Seed eval/sets/qa.jsonl from eval/datasets/golden.json.

Every golden item becomes a fuzzy item. Where one true_data file clearly contains the reference text
(containment above MIN_SCORE and ahead of the runner-up by MIN_MARGIN), it is recorded as a
calibration source, making the item dual-labelled. Review the printed table before relying on it.
"""
import json
import sys
from pathlib import Path

# Scripts run by path start with scripts/ on sys.path, not the repo root, so the eval package would not import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.harness.corpus import TRUE_DIR, extract_text  # noqa: E402
from eval.harness.stats import containment  # noqa: E402

GOLDEN = Path("eval/datasets/golden.json")
OUT = Path("eval/sets/qa.jsonl")
MIN_SCORE = 0.6
MIN_MARGIN = 0.05


def main():
    if OUT.exists():
        print(f"{OUT} already exists; delete it to re-seed", file=sys.stderr)
        return 1
    texts = {p.name: extract_text(p) for p in sorted(Path(TRUE_DIR).iterdir()) if p.is_file()}
    rows, review = [], []
    for n, item in enumerate(json.loads(GOLDEN.read_text()), start=1):
        reference = " ".join([item["ground_truth"], *item["contexts"]])
        ranked = sorted(((containment(reference, text), name) for name, text in texts.items()), reverse=True)
        (best, best_name), (second, _) = ranked[0], ranked[1]
        confident = best >= MIN_SCORE and best - second >= MIN_MARGIN
        item_id = f"g{n:03d}"
        rows.append({"id": item_id, "query": item["question"], "origin": "golden", "answerable": True,
                     "label_type": "fuzzy", "reference_contexts": item["contexts"],
                     "calibration_sources": [best_name] if confident else [],
                     "ground_truth": item["ground_truth"], "tags": ["golden"]})
        review.append((item_id, best_name, best, second, "yes" if confident else "NO"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{'id':<6}{'best file':<28}{'score':>7}{'second':>8}  calibrated")
    for item_id, name, best, second, flag in review:
        print(f"{item_id:<6}{name:<28}{best:>7.2f}{second:>8.2f}  {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
