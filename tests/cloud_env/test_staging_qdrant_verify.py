"""A healthy endpoint or 200 stream alone is not proof of recovered data."""

from uuid import UUID

import pytest

from scripts.staging_qdrant.aws_storage import RecoveryError
from scripts.staging_qdrant.verify import verify_cluster, verify_staging_api


def good_qdrant(method, path, payload=None):
    if path == "/cluster":
        return {"status": "ok", "result": {"status": "enabled", "peers": {
            "1": {}, "2": {}, "3": {},
        }}}
    if path == "/collections/rag_collection":
        return {"status": "ok", "result": {
            "status": "green", "points_count": 7,
            "config": {"params": {"vectors": {"size": 1024, "distance": "Cosine"}}},
        }}
    if path == "/collections/rag_collection/cluster":
        return {"status": "ok", "result": {
            "shard_count": 1, "local_shards": [{"shard_id": 0, "state": "Active"}],
            "remote_shards": [{"shard_id": 0, "peer_id": 2, "state": "Active"}],
            "shard_transfers": [],
        }}
    if path.endswith("/points/scroll"):
        return {"status": "ok", "result": {"points": [{"id": 1}]}}
    if path.endswith("/points/search"):
        assert payload["vector"][0] == 1.0
        assert len(payload["vector"]) == 1024
        return {"status": "ok", "result": [{"id": 1, "score": 0.9}]}
    raise AssertionError(path)


def test_retained_cluster_needs_nonzero_points_and_active_shards():
    def no_points(method, path, payload=None):
        result = good_qdrant(method, path, payload)
        if path == "/collections/rag_collection":
            result["result"]["points_count"] = 0
        return result

    with pytest.raises(RecoveryError, match="points"):
        verify_cluster(no_points, retained=True)

    def no_shards(method, path, payload=None):
        result = good_qdrant(method, path, payload)
        if path.endswith("/cluster") and path != "/cluster":
            result["result"]["local_shards"] = []
            result["result"]["remote_shards"] = []
        return result

    with pytest.raises(RecoveryError, match="shard"):
        verify_cluster(no_shards, retained=True)


@pytest.mark.parametrize("defect", ["peer", "size", "distance", "state"])
def test_wrong_cluster_structure_is_rejected(defect):
    def broken(method, path, payload=None):
        result = good_qdrant(method, path, payload)
        if defect == "peer" and path == "/cluster":
            result["result"]["peers"].pop("3")
        if defect == "size" and path == "/collections/rag_collection":
            result["result"]["config"]["params"]["vectors"]["size"] = 768
        if defect == "distance" and path == "/collections/rag_collection":
            result["result"]["config"]["params"]["vectors"]["distance"] = "Dot"
        if defect == "state" and path == "/collections/rag_collection/cluster":
            result["result"]["local_shards"][0]["state"] = "Dead"
        return result

    with pytest.raises(RecoveryError):
        verify_cluster(broken, retained=True)


def test_good_cluster_proves_non_content_scroll_and_search():
    report = verify_cluster(good_qdrant, retained=True)
    assert report.ok
    assert report.peer_count == 3
    assert report.point_count == 7
    assert report.query_ok


def stream(events):
    return lambda payload, headers: {
        "status": 200, "headers": {"x-run-id": "00000000-0000-4000-8000-000000000001"},
        "events": events,
    }


def evidence_with_retrieval(run_id):
    assert UUID(run_id)
    return {"safe_run": {"run_id": run_id, "retrieval": [{
        "collection": "rag_collection", "vector_status": "success", "items": [{"rank": 1}],
    }]}}


def test_stream_200_with_error_event_is_not_verified():
    events = [{"type": "routing", "action": "retrieve"},
              {"type": "error", "content": "An internal error occurred."}]
    with pytest.raises(RecoveryError, match="staging retrieval"):
        verify_staging_api(stream(events), evidence_with_retrieval, token="test-token", retained=True)


def test_cached_answer_without_correlated_retrieval_is_not_verified():
    events = [{"type": "routing", "action": "retrieve"}, {"type": "answer", "content": "cached"}]
    with pytest.raises(RecoveryError, match="retrieval evidence"):
        verify_staging_api(stream(events), lambda run_id: {"safe_run": {"run_id": run_id, "retrieval": []}},
                           token="test-token", retained=True)


def test_api_verification_requires_cache_bypass_and_matching_run_id():
    sent = []

    def api_post(payload, headers):
        sent.append((payload, headers))
        return stream([{"type": "routing", "action": "retrieve"},
                       {"type": "answer", "content": "grounded"}])(payload, headers)

    verify_staging_api(api_post, evidence_with_retrieval, token="test-token", retained=True,
                       cache_bypass_enabled=True)
    assert sent[0][1]["X-Load-Test-Bypass-Cache"] == "1"
    assert sent[0][1]["Authorization"] == "Bearer test-token"
    assert sent[0][0]["session_id"]
    with pytest.raises(RecoveryError, match="cache bypass"):
        verify_staging_api(api_post, evidence_with_retrieval, token="test-token", retained=True,
                           cache_bypass_enabled=False)
