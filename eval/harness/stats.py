import math
import re

_STOPWORDS = frozenset(
    "the and for are was with that this from into its has have can not but you all any one how what when "
    "where which who will each also than then them they their there these those been being such".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


def wilson_interval(successes, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def intervals_overlap(a, b):
    return a[0] <= b[1] and b[0] <= a[1]


def auroc(positive, negative):
    if not positive or not negative:
        return None
    wins = 0.0
    for p in positive:
        for n in negative:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positive) * len(negative))


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(idx), math.ceil(idx)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def content_tokens(text):
    return {t for t in _TOKEN.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS}


def containment(reference, chunk):
    ref = content_tokens(reference)
    if not ref:
        return 0.0
    return len(ref & content_tokens(chunk)) / len(ref)
