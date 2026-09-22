import httpx

from eval.harness.recorders import normalize_payload


async def llm_probe():
    from services.api.app.clients.ray_llm import llm_client

    await llm_client.chat_completion(
        messages=[{"role": "user", "content": "Reply with the word ok."}], temperature=0.0, max_tokens=8)


async def embed_probe():
    from services.api.app.config import settings

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(settings.RAY_EMBED_ENDPOINT,
                                     json={"model": settings.EMBED_MODEL_NAME, "prompt": "warm-up"})
        response.raise_for_status()
        return len(response.json()["embedding"])


async def qdrant_probe():
    from services.api.app.clients.qdrant import qdrant_client
    from services.api.app.config import settings

    client, name = qdrant_client.client, settings.QDRANT_COLLECTION
    existing = {c.name for c in (await client.get_collections()).collections}
    if name not in existing:
        return {"exists": False, "points": 0, "vector_size": None, "filenames": set()}
    info = await client.get_collection(name)
    filenames, offset = set(), None
    while True:
        points, offset = await client.scroll(
            collection_name=name, limit=256, offset=offset, with_payload=["filename", "metadata"], with_vectors=False)
        filenames.update(normalize_payload(p.payload)[0] for p in points)
        if offset is None:
            break
    return {"exists": True, "points": info.points_count,
            "vector_size": getattr(info.config.params.vectors, "size", None), "filenames": filenames}


async def neo4j_probe():
    from services.api.app.clients.neo4j import neo4j_client

    rows = await neo4j_client.query("MATCH (n) RETURN count(n) AS c")
    return rows[0]["c"]
