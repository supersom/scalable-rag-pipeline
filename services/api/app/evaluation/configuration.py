"""Fingerprint known runtime configuration without pretending to pin a corpus."""
import inspect

from services.api.app.config import settings
from .context import content_hash
from .models import ConfigurationFingerprint


def configuration_fingerprint():
    from services.api.app.agents import graph
    from services.api.app.agents.nodes import planner, responder
    from services.api.app.tools.registry import TOOL_REGISTRY
    return ConfigurationFingerprint(
        agent_version=None if settings.OTEL_SERVICE_VERSION == "unknown" else settings.OTEL_SERVICE_VERSION,
        agent_build_sha=settings.AGENT_BUILD_SHA,
        generation_model=settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME,
        embedding_model=settings.EMBED_MODEL_NAME, embedding_dimensions=settings.EMBED_DIM,
        graph_version=content_hash(inspect.getsource(graph)),
        prompt_hashes={"planner": content_hash(planner.SYSTEM_PROMPT), "responder_template_source": content_hash(inspect.getsource(responder._generate))},
        tool_registry_hash=content_hash({name: {"description": tool["description"], "cacheable": tool.get("cacheable")}
                                         for name, tool in TOOL_REGISTRY.items()}),
    )
