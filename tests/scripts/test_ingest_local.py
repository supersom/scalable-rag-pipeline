from scripts.ingest_local import resolve_llm_model


def test_resolve_llm_model_prefers_llm_model_id():
    env = {"LLM_MODEL_ID": "nvidia/nemotron-3-ultra-550b-a55b:free", "LLM_MODEL_NAME": "llama3.2:3b"}
    assert resolve_llm_model(env) == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_resolve_llm_model_falls_back_to_llm_model_name():
    env = {"LLM_MODEL_NAME": "qwen3.5:latest"}
    assert resolve_llm_model(env) == "qwen3.5:latest"


def test_resolve_llm_model_defaults_when_neither_set():
    assert resolve_llm_model({}) == "llama3.2:3b"


def test_resolve_llm_model_ignores_empty_llm_model_id():
    env = {"LLM_MODEL_ID": "", "LLM_MODEL_NAME": "llama3.2:3b"}
    assert resolve_llm_model(env) == "llama3.2:3b"
