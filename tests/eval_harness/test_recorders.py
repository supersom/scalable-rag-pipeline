import asyncio
import types

import pytest

from eval.harness.recorders import PlannerRecorder, RecorderError, RetrievalRecorder, normalize_payload


def test_normalize_payload_flat_nested_and_missing():
    assert normalize_payload({"filename": "a.pdf", "page": 3, "text": "t"}) == ("a.pdf", 3, "t")
    assert normalize_payload({"text": "t", "metadata": {"filename": "b.pdf", "page_number": 7}}) == ("b.pdf", 7, "t")
    assert normalize_payload(None) == ("unknown", None, "")


def make_module(fn):
    return types.SimpleNamespace(_extract_json=fn)


def test_planner_recorder_captures_raw_plan_and_passes_through():
    module = make_module(lambda text: {"action": "retrieve", "tool_name": None})
    recorder = PlannerRecorder(module)
    with recorder:
        assert module._extract_json("x") == {"action": "retrieve", "tool_name": None}
    assert recorder.last() == {"action": "retrieve", "tool_name": None}


def test_planner_recorder_records_parse_error_and_reraises():
    def boom(text):
        raise ValueError("no json")

    module = make_module(boom)
    recorder = PlannerRecorder(module)
    with recorder:
        with pytest.raises(ValueError):
            module._extract_json("x")
    assert recorder.last() == {"_error": "no json"}


def test_planner_recorder_restores_original_and_resets():
    def original(text):
        return {}

    module = make_module(original)
    recorder = PlannerRecorder(module)
    with recorder:
        assert module._extract_json is not original
        module._extract_json("x")
        recorder.reset()
        assert recorder.last() is None
    assert module._extract_json is original


def test_planner_recorder_fails_loudly_when_function_is_missing():
    with pytest.raises(RecorderError, match="_extract_json"):
        PlannerRecorder(types.SimpleNamespace())


class FakeClient:
    def __init__(self, results):
        self.results = results

    async def search(self, vector, limit=5):
        return self.results


def point(payload, score):
    return types.SimpleNamespace(payload=payload, score=score)


def test_retrieval_recorder_records_ranked_hits_and_passes_result_through():
    results = [
        point({"filename": "a.pdf", "page": 1, "text": "alpha"}, 0.9),
        point({"text": "beta", "metadata": {"filename": "b.pdf", "page_number": 2}}, 0.5),
    ]
    client = FakeClient(results)
    recorder = RetrievalRecorder(client)
    with recorder:
        returned = asyncio.run(client.search([0.1], limit=5))
    assert returned is results
    assert recorder.last() == [
        {"filename": "a.pdf", "page": 1, "score": 0.9, "text": "alpha"},
        {"filename": "b.pdf", "page": 2, "score": 0.5, "text": "beta"},
    ]


def test_retrieval_recorder_stops_recording_after_exit():
    client = FakeClient([point({"filename": "a.pdf", "text": "t"}, 0.1)])
    recorder = RetrievalRecorder(client)
    with recorder:
        pass
    recorder.reset()
    asyncio.run(client.search([0.1]))
    assert recorder.last() is None


def test_retrieval_recorder_rejects_sync_search():
    class SyncClient:
        def search(self, vector, limit=5):
            return []

    with pytest.raises(RecorderError, match="async"):
        RetrievalRecorder(SyncClient())


def test_planner_recorder_rejects_double_install():
    module = make_module(lambda text: {"action": "retrieve"})
    recorder = PlannerRecorder(module)
    recorder.install()
    with pytest.raises(RecorderError):
        recorder.install()


def test_planner_recorder_uninstall_fully_restores_after_failed_double_install():
    def original(text):
        return {}

    module = make_module(original)
    recorder = PlannerRecorder(module)
    recorder.install()
    try:
        recorder.install()
    except RecorderError:
        pass
    recorder.uninstall()
    assert module._extract_json is original


def test_retrieval_recorder_rejects_double_install():
    client = FakeClient([point({"filename": "a.pdf", "text": "t"}, 0.1)])
    recorder = RetrievalRecorder(client)
    recorder.install()
    with pytest.raises(RecorderError):
        recorder.install()
