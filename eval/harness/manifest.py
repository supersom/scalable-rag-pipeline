import hashlib
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse

COMPARABLE_KEYS = ("model", "embed_model", "dataset_hashes", "corpus_hash")


def endpoint_host(url):
    return urlparse(url).hostname or ""


def planner_prompt_hash(system_prompt):
    return hashlib.sha256(system_prompt.encode()).hexdigest()


def git_info(run=subprocess.run):
    sha = run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    status = run(["git", "status", "--porcelain", "--untracked-files=no"],
                 capture_output=True, text=True, check=True).stdout.strip()
    return sha, bool(status)


def build_manifest(*, layer, model, endpoint, embed_model, embed_dim, prompt_hash, git_sha, git_dirty,
                   dataset_hashes, corpus_hash, corpus_seed, qdrant_points, qdrant_vector_size, neo4j_nodes,
                   k=5, judge=None, now=None):
    return {
        "layer": layer,
        "started_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "endpoint_host": endpoint_host(endpoint),
        "embed_model": embed_model,
        "embed_dim": embed_dim,
        "planner_prompt_hash": prompt_hash,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "dataset_hashes": dataset_hashes,
        "corpus_hash": corpus_hash,
        "corpus_seed": corpus_seed,
        "qdrant_points": qdrant_points,
        "qdrant_vector_size": qdrant_vector_size,
        "neo4j_nodes": neo4j_nodes,
        "k": k,
        "judge": judge,
    }


def comparability(a, b):
    return [key for key in COMPARABLE_KEYS if a.get(key) != b.get(key)]
