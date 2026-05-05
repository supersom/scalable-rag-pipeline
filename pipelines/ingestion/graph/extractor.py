# pipelines/ingestion/graph/extractor.py
import json
import os
import httpx
from typing import Dict, Any

from pipelines.ingestion.graph.schema import GraphSchema

LLM_ENDPOINT = os.getenv("RAY_LLM_ENDPOINT", "http://localhost:11434/v1/chat/completions")
LLM_MODEL    = os.getenv("LLM_MODEL_NAME",   "llama3.2:3b")


class GraphExtractor:
    """
    Ray Actor Class for Graph Extraction.
    Calls the LLM to extract (Subject, Predicate, Object) triples from each chunk.
    Locally points at Ollama; in prod points at Ray Serve.
    """
    def __init__(self):
        self.client = httpx.Client(timeout=120.0)

    def __call__(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        nodes_list = []
        edges_list = []

        for text in batch["text"]:
            try:
                prompt = GraphSchema.get_system_prompt() + f"\n\nInput Text:\n{text}"

                response = self.client.post(
                    LLM_ENDPOINT,
                    json={
                        "model": LLM_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.0,
                        "max_tokens": 1024,
                    }
                )
                response.raise_for_status()

                content = response.json()["choices"][0]["message"]["content"]
                # Strip markdown code fences if the model wraps the JSON
                content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                graph_data = json.loads(content)

                nodes_list.append(graph_data.get("nodes", []))
                edges_list.append(graph_data.get("edges", []))

            except Exception as e:
                print(f"Graph extraction failed for chunk: {e}")
                nodes_list.append([])
                edges_list.append([])

        batch["graph_nodes"] = nodes_list
        batch["graph_edges"] = edges_list
        return batch
