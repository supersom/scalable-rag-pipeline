import pytest

from eval.harness.stats import auroc, containment, content_tokens, intervals_overlap, percentile, wilson_interval


def test_wilson_known_values():
    lo, hi = wilson_interval(8, 10)
    assert lo == pytest.approx(0.490, abs=0.002) and hi == pytest.approx(0.943, abs=0.002)
    assert wilson_interval(0, 10) == (0.0, pytest.approx(0.2775, abs=0.002))
    assert wilson_interval(10, 10) == (pytest.approx(0.7225, abs=0.002), 1.0)


def test_wilson_empty_sample_is_uninformative():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_intervals_overlap():
    assert intervals_overlap((0.1, 0.5), (0.4, 0.9))
    assert intervals_overlap((0.1, 0.5), (0.5, 0.9))
    assert not intervals_overlap((0.1, 0.4), (0.5, 0.9))


def test_auroc():
    assert auroc([0.9, 0.8], [0.5, 0.4]) == 1.0
    assert auroc([0.5], [0.5]) == 0.5
    assert auroc([0.4], [0.9]) == 0.0
    assert auroc([], [0.1]) is None and auroc([0.1], []) is None


def test_percentile():
    assert percentile([1, 2, 3, 4, 5], 50) == 3.0
    assert percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
    assert percentile([7], 95) == 7.0
    assert percentile([], 50) is None


def test_content_tokens_drops_stopwords_and_short_words():
    assert content_tokens("The Pod is an HPA target, and it has 3 replicas") == {"pod", "hpa", "target", "replicas"}


def test_containment():
    reference = "Horizontal Pod Autoscaler adjusts replicas"
    assert containment(reference, "The horizontal pod autoscaler adjusts the number of replicas") == 1.0
    assert containment(reference, "The horizontal pod autoscaler adjusts the number") == pytest.approx(0.8)
    assert containment(reference, "assembly language registers") == 0.0
    assert containment("", "anything") == 0.0
