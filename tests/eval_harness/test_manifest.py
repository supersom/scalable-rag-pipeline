import types

from eval.harness.manifest import (
    COMPARABLE_KEYS, build_manifest, comparability, endpoint_host, git_info, planner_prompt_hash,
)


def test_endpoint_host_drops_path_and_credentials():
    assert endpoint_host("https://user:pw@openrouter.ai/api/v1/chat/completions") == "openrouter.ai"
    assert endpoint_host("http://localhost:11434/api/embeddings") == "localhost"


def test_prompt_hash_changes_with_prompt():
    assert planner_prompt_hash("a") == planner_prompt_hash("a")
    assert planner_prompt_hash("a") != planner_prompt_hash("b") and len(planner_prompt_hash("a")) == 64


def fake_run(outputs):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(stdout=outputs[cmd[1]])

    run.calls = calls
    return run


def test_git_info_reports_sha_and_dirty_flag_ignoring_untracked():
    dirty = fake_run({"rev-parse": "abc123\n", "status": " M file.py\n"})
    assert git_info(dirty) == ("abc123", True)
    assert "--untracked-files=no" in dirty.calls[1]
    assert git_info(fake_run({"rev-parse": "abc123\n", "status": ""})) == ("abc123", False)


def manifest(**over):
    fields = dict(layer="routing", model="m", endpoint="https://openrouter.ai/api/v1", embed_model="nomic",
                  embed_dim=768, prompt_hash="p", git_sha="abc", git_dirty=False, dataset_hashes={"routing.jsonl": "h"},
                  corpus_hash=None, corpus_seed=None, qdrant_points=None, qdrant_vector_size=None, neo4j_nodes=None,
                  now="2026-09-21T10:00:00+00:00")
    fields.update(over)
    return build_manifest(**fields)


def test_build_manifest_records_fields_and_defaults():
    m = manifest()
    assert m["endpoint_host"] == "openrouter.ai" and "endpoint" not in m
    assert m["k"] == 5 and m["judge"] is None and m["started_at"] == "2026-09-21T10:00:00+00:00"
    assert m["planner_prompt_hash"] == "p" and m["dataset_hashes"] == {"routing.jsonl": "h"}


def test_comparability_flags_only_comparable_keys():
    base = manifest()
    assert comparability(base, manifest(git_sha="def", prompt_hash="q")) == []
    assert comparability(base, manifest(model="other", corpus_hash="c")) == ["model", "corpus_hash"]
    assert set(COMPARABLE_KEYS) == {"model", "embed_model", "dataset_hashes", "corpus_hash"}
