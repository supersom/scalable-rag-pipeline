"""Data and run-correlated API gates for the staging Qdrant handoff."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from .aws_storage import RecoveryError


COLLECTION = "rag_collection"
VECTOR_SIZE = 1024


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    peer_count: int
    collection_status: str
    vector_size: int
    distance: str
    point_count: int
    query_ok: bool


def _result(response: dict, operation: str) -> dict | list:
    if not isinstance(response, dict) or response.get("status") != "ok" or "result" not in response:
        raise RecoveryError(f"Qdrant {operation} did not succeed")
    return response["result"]


def verify_cluster(qdrant_request, *, retained: bool) -> VerificationReport:
    """Verify three peers, shard health, schema, and non-content read access."""
    cluster = _result(qdrant_request("GET", "/cluster"), "cluster status")
    peers = cluster.get("peers") if isinstance(cluster, dict) else None
    if cluster.get("status") != "enabled" or not isinstance(peers, dict) or len(peers) != 3:
        raise RecoveryError("Qdrant does not have three enabled peers")

    collection = _result(qdrant_request("GET", f"/collections/{COLLECTION}"), "collection status")
    if not isinstance(collection, dict) or collection.get("status") != "green":
        raise RecoveryError("Qdrant collection is not green")
    vectors = collection.get("config", {}).get("params", {}).get("vectors")
    if not isinstance(vectors, dict) or vectors.get("size") != VECTOR_SIZE or vectors.get("distance") != "Cosine":
        raise RecoveryError("Qdrant collection vector schema is not 1024/Cosine")
    points = collection.get("points_count")
    if not isinstance(points, int) or points < 0 or (retained and points == 0):
        raise RecoveryError("Qdrant retained collection has no verified points")

    placement = _result(qdrant_request("GET", f"/collections/{COLLECTION}/cluster"), "shard placement")
    if not isinstance(placement, dict) or not isinstance(placement.get("shard_count"), int) or placement["shard_count"] < 1:
        raise RecoveryError("Qdrant collection shard count is missing")
    shards = placement.get("local_shards", []) + placement.get("remote_shards", [])
    if not shards or len({shard.get("shard_id") for shard in shards}) < placement["shard_count"]:
        raise RecoveryError("Qdrant collection shards are missing")
    if any(shard.get("state") != "Active" for shard in shards) or placement.get("shard_transfers"):
        raise RecoveryError("Qdrant collection has an unavailable shard or transfer")

    scrolled = _result(qdrant_request("POST", f"/collections/{COLLECTION}/points/scroll", {
        "limit": 1, "with_payload": False, "with_vector": False,
    }), "scroll")
    scroll_points = scrolled.get("points") if isinstance(scrolled, dict) else None
    if not isinstance(scroll_points, list) or (retained and not scroll_points):
        raise RecoveryError("Qdrant retained collection scroll returned no points")
    vector = [1.0] + [0.0] * (VECTOR_SIZE - 1)
    searched = _result(qdrant_request("POST", f"/collections/{COLLECTION}/points/search", {
        "vector": vector, "limit": 1, "with_payload": False, "with_vector": False,
    }), "search")
    if not isinstance(searched, list) or (retained and not searched):
        raise RecoveryError("Qdrant retained collection search returned no points")
    return VerificationReport(True, len(peers), collection["status"], vectors["size"],
                              vectors["distance"], points, True)


def verify_staging_api(
    api_post, evidence_lookup, *, token: str, retained: bool,
    cache_bypass_enabled: bool = True,
) -> str:
    """Require a non-cached retrieval and its persisted safe evidence by run ID."""
    if not cache_bypass_enabled:
        raise RecoveryError("staging cache bypass is disabled")
    if not token:
        raise RecoveryError("staging API verification token is missing")
    response = api_post(
        {"message": "What is the warranty policy for the new X1 processor?", "session_id": str(uuid4())},
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
         "X-Load-Test-Bypass-Cache": "1"},
    )
    if response.get("status") != 200:
        raise RecoveryError("staging retrieval request did not return HTTP 200")
    events = response.get("events")
    if (not isinstance(events, list) or any(event.get("type") == "error" for event in events)
            or not any(event.get("type") == "routing" and event.get("action") == "retrieve" for event in events)
            or not any(event.get("type") == "answer" and event.get("content") for event in events)):
        raise RecoveryError("staging retrieval did not complete with an answer")
    run_id = response.get("headers", {}).get("x-run-id")
    try:
        UUID(str(run_id))
    except (ValueError, TypeError) as exc:
        raise RecoveryError("staging retrieval did not provide a valid run ID") from exc
    envelope = evidence_lookup(run_id)
    safe_run = envelope.get("safe_run", {}) if isinstance(envelope, dict) else {}
    retrievals = safe_run.get("retrieval", [])
    if safe_run.get("run_id") != run_id or not any(
        item.get("collection") == COLLECTION and item.get("vector_status") == "success"
        and (not retained or item.get("items"))
        for item in retrievals if isinstance(item, dict)
    ):
        raise RecoveryError("correlated retrieval evidence is absent")
    return run_id
