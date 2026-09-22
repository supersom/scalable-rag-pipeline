from dataclasses import dataclass, field


@dataclass
class PreflightResult:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    @property
    def ok(self):
        return not self.errors


async def run_preflight(*, layer, embed_dim, expected_sources, probes, collection="rag_collection"):
    result = PreflightResult()
    if layer == "routing":
        try:
            await probes["llm"]()
        except Exception as e:
            result.errors.append(f"LLM endpoint failed ({type(e).__name__}: {e}). Check RAY_LLM_ENDPOINT and LLM_API_KEY in .env.")
        return result

    actual_dim = None
    try:
        actual_dim = await probes["embed"]()
    except Exception as e:
        result.errors.append(f"Embed endpoint failed ({type(e).__name__}: {e}). Check RAY_EMBED_ENDPOINT and that Ollama is running.")
    else:
        result.facts["embed_dim_actual"] = actual_dim
        if actual_dim != embed_dim:
            result.errors.append(f"Embed endpoint returns {actual_dim}-dim vectors but EMBED_DIM={embed_dim}. Fix EMBED_DIM in .env.")

    info = None
    try:
        info = await probes["qdrant"]()
    except Exception as e:
        result.errors.append(f"Qdrant unreachable ({type(e).__name__}: {e}). Run: docker compose up -d qdrant")
    if info is not None:
        result.facts.update(qdrant_points=info["points"], qdrant_vector_size=info["vector_size"],
                            filenames=info["filenames"])
        if not info["exists"]:
            result.errors.append(
                f"Collection '{collection}' does not exist. Create it: curl -X PUT localhost:6333/collections/{collection} "
                f"-H 'Content-Type: application/json' -d '{{\"vectors\":{{\"size\":{embed_dim},\"distance\":\"Cosine\"}}}}'")
        elif actual_dim is not None and info["vector_size"] != actual_dim:
            result.errors.append(
                f"Collection '{collection}' has vector size {info['vector_size']} but embeddings are {actual_dim}-dim. "
                "Delete and recreate the collection, then re-ingest.")
        elif info["points"] == 0:
            result.errors.append(f"Collection '{collection}' is empty. Ingest the corpus first.")
        else:
            missing = sorted(set(expected_sources) - info["filenames"])
            result.facts["missing_sources"] = missing
            if missing:
                result.warnings.append(f"{len(missing)} expected source(s) not ingested; their items will be unscorable: {missing}")

    try:
        nodes = await probes["neo4j"]()
    except Exception:
        nodes = None
    result.facts["neo4j_nodes"] = nodes
    if nodes is None:
        result.warnings.append("Neo4j unavailable: the graph arm returns no documents.")
    elif nodes == 0:
        result.warnings.append("Neo4j is up but empty: the graph arm returns no documents.")
    return result
