import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from eval.harness.stats import auroc, containment, percentile

K_VALUES = (1, 3, 5)
DEFAULT_FUZZY_THRESHOLD = 0.5
FUZZY_RELIABLE_AGREEMENT = 0.85
UNANSWERABLE_MIN_N = 20
CALIBRATION_CANDIDATES = tuple(round(0.1 + 0.05 * i, 2) for i in range(17))
_SOURCE_SUFFIX = re.compile(r"\[Source: .*\]\s*$")


@dataclass
class RetrievalResult:
    item_id: str
    verdict: str
    ranked: list = field(default_factory=list)
    documents_n: int = 0
    vector_docs: int = 0
    graph_docs: int = 0
    top1_score: Optional[float] = None
    latency_ms: Optional[float] = None
    reason: Optional[str] = None


def arm_counts(documents):
    vector = sum(1 for d in documents if _SOURCE_SUFFIX.search(d))
    return vector, len(documents) - vector


def hit_at_k(filenames, expected, k):
    return any(f in expected for f in filenames[:k])


def reciprocal_rank(filenames, expected):
    for rank, name in enumerate(filenames, start=1):
        if name in expected:
            return 1.0 / rank
    return 0.0


def source_recall(filenames, expected, k=5):
    return len({f for f in filenames[:k] if f in expected}) / len(set(expected))


def chunk_relevant(chunk, contexts, threshold):
    return max((containment(ref, chunk["text"]) for ref in contexts), default=0.0) >= threshold


def fuzzy_hit_at_k(chunks, contexts, threshold, k):
    return any(chunk_relevant(c, contexts, threshold) for c in chunks[:k])


async def run_retrieval(items, retrieve_fn, recorder, present_sources):
    results = []
    for item in items:
        if item.label_type == "source":
            missing = [s for s in item.expected_sources if s not in present_sources]
            if missing:
                results.append(RetrievalResult(item_id=item.id, verdict="unscorable",
                                               reason=f"sources not ingested: {missing}"))
                continue
        recorder.reset()
        started = time.perf_counter()
        try:
            out = await retrieve_fn({"current_query": item.query})
        except Exception as e:
            results.append(RetrievalResult(item_id=item.id, verdict="error", reason=f"{type(e).__name__}: {e}",
                                           latency_ms=(time.perf_counter() - started) * 1000))
            continue
        latency_ms = (time.perf_counter() - started) * 1000
        ranked = recorder.last() or []
        documents = out.get("documents", [])
        vector, graph = arm_counts(documents)
        results.append(RetrievalResult(
            item_id=item.id, verdict="scored", ranked=ranked, documents_n=len(documents), vector_docs=vector,
            graph_docs=graph, top1_score=ranked[0]["score"] if ranked else None, latency_ms=latency_ms))
    return results


def calibrate_threshold(items, results, candidates=CALIBRATION_CANDIDATES):
    scored = {r.item_id: r for r in results if r.verdict == "scored"}
    dual = [(i, scored[i.id]) for i in items if i.label_type == "fuzzy" and i.calibration_sources and i.id in scored]
    pairs = [(item, chunk) for item, res in dual for chunk in res.ranked[:5]]
    if not pairs:
        return DEFAULT_FUZZY_THRESHOLD, None, len(dual)
    best_threshold, best_rate = candidates[0], -1.0
    for threshold in candidates:
        agree = sum(chunk_relevant(c, item.reference_contexts, threshold) == (c["filename"] in item.calibration_sources)
                    for item, c in pairs)
        rate = agree / len(pairs)
        if rate >= best_rate:
            best_threshold, best_rate = threshold, rate
    return best_threshold, best_rate, len(dual)


def _mean(values):
    return sum(values) / len(values) if values else None


def _youden_cutoff(positive, negative):
    best, best_j = None, -1.0
    for t in sorted(set(positive + negative)):
        j = sum(p >= t for p in positive) / len(positive) - sum(n >= t for n in negative) / len(negative)
        if j > best_j:
            best, best_j = t, j
    return best


def summarize_retrieval(items, results, threshold, agreement, n_calibration):
    by_id = {i.id: i for i in items}
    scored = [(by_id[r.item_id], r) for r in results if r.verdict == "scored"]

    def names(r):
        return [c["filename"] for c in r.ranked]

    source = [(i, r) for i, r in scored if i.label_type == "source"]
    headline = {}
    if source:
        for k in K_VALUES:
            headline[f"source_hit@{k}"] = {
                "successes": sum(hit_at_k(names(r), set(i.expected_sources), k) for i, r in source), "n": len(source)}

    grouped = defaultdict(list)
    for i, r in scored:
        grouped[f"{i.label_type or 'unanswerable'}/{i.origin}"].append((i, r))
    groups = {}
    for key, rows in sorted(grouped.items()):
        entry = {"n": len(rows)}
        label = key.split("/")[0]
        if label == "source":
            for k in K_VALUES:
                entry[f"hit@{k}"] = sum(hit_at_k(names(r), set(i.expected_sources), k) for i, r in rows) / len(rows)
            entry["mrr"] = _mean([reciprocal_rank(names(r), set(i.expected_sources)) for i, r in rows])
            entry["source_recall@5"] = _mean([source_recall(names(r), set(i.expected_sources)) for i, r in rows])
        elif label == "fuzzy":
            for k in K_VALUES:
                entry[f"hit@{k}"] = sum(fuzzy_hit_at_k(r.ranked, i.reference_contexts, threshold, k)
                                        for i, r in rows) / len(rows)
        else:
            entry["mean_top1_score"] = _mean([r.top1_score for _, r in rows if r.top1_score is not None])
        groups[key] = entry

    positives = [r.top1_score for i, r in scored if i.answerable and r.top1_score is not None]
    negatives = [r.top1_score for i, r in scored if not i.answerable and r.top1_score is not None]
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    return {
        "headline": headline,
        "groups": groups,
        "fuzzy": {"n": sum(1 for i, _ in scored if i.label_type == "fuzzy"), "threshold": threshold,
                  "agreement": agreement, "n_calibration": n_calibration,
                  "reliable": agreement is not None and agreement >= FUZZY_RELIABLE_AGREEMENT},
        "unanswerable": {"n_answerable": len(positives), "n_unanswerable": len(negatives),
                         "auroc": auroc(positives, negatives),
                         "cutoff": _youden_cutoff(positives, negatives) if positives and negatives else None,
                         "descriptive_only": len(negatives) < UNANSWERABLE_MIN_N},
        "empty_context_rate": sum(r.documents_n == 0 for _, r in scored) / len(scored) if scored else None,
        "arms": {"vector_docs": sum(r.vector_docs for _, r in scored), "graph_docs": sum(r.graph_docs for _, r in scored)},
        "unscorable": [r.item_id for r in results if r.verdict == "unscorable"],
        "errors": sum(r.verdict == "error" for r in results),
        "invalid": False,
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "n_items": len(items),
    }
