"""The staging deploy's dotenv loader follows dotenv value semantics."""

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deploy_staging_evidence.sh"


def load(tmp_path, body: str) -> dict[str, str]:
    loop = re.search(r"while IFS= read -r env_line.*?done < \"\$STAGING_ENV_FILE\"", SCRIPT.read_text(), re.S)
    assert loop, "dotenv loop not found in deploy script"
    env_file = tmp_path / "env"
    env_file.write_text(body)
    keys = ["EMBED_MODEL_NAME", "EMBED_DIM", "LLM_MODEL_NAME", "LLM_API_KEY", "TAVILY_API_KEY", "JWT_SECRET_KEY"]
    out = subprocess.run(
        ["bash", "-c", f'STAGING_ENV_FILE="{env_file}"\n{loop.group(0)}\n'
         + "".join(f'printf "%s=[%s]\\n" {k} "${{{k}:-}}"\n' for k in keys)],
        text=True, capture_output=True, check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


def test_inline_comment_is_not_part_of_an_unquoted_value(tmp_path):
    values = load(tmp_path, "EMBED_MODEL_NAME=baai/bge-m3 # fake-local-embed\nEMBED_DIM=1024   #dim\n")
    assert values["EMBED_MODEL_NAME"] == "[baai/bge-m3]"
    assert values["EMBED_DIM"] == "[1024]"


def test_hash_without_leading_space_and_quoted_values_are_preserved(tmp_path):
    values = load(tmp_path, 'LLM_API_KEY=sk#abc\nLLM_MODEL_NAME="a model # kept" # trailing\n')
    assert values["LLM_API_KEY"] == "[sk#abc]"
    assert values["LLM_MODEL_NAME"] == "[a model # kept]"


def test_single_quotes_and_plain_values(tmp_path):
    values = load(tmp_path, "LLM_MODEL_NAME='x y'\nEMBED_MODEL_NAME=plain\n")
    assert values["LLM_MODEL_NAME"] == "[x y]"
    assert values["EMBED_MODEL_NAME"] == "[plain]"


def test_tavily_key_is_loaded_but_unlisted_secrets_are_not(tmp_path):
    values = load(tmp_path, "TAVILY_API_KEY=tvly-abc # web search\nJWT_SECRET_KEY=must-not-load\n")
    assert values["TAVILY_API_KEY"] == "[tvly-abc]"
    assert values["JWT_SECRET_KEY"] == "[]"


def test_deploy_stores_the_tavily_key_in_the_staging_secret():
    script = SCRIPT.read_text()
    assert re.search(r"for provider_key in [^;]*\bTAVILY_API_KEY\b[^;]*; do", script)


def test_existing_api_deployment_is_restarted_after_the_helm_upgrade():
    script = SCRIPT.read_text()
    check = script.index("API_ALREADY_DEPLOYED=false")
    upgrade = script.index("helm upgrade --install api-staging")
    restart = script.index("kubectl rollout restart deployment/api-staging")
    assert check < upgrade < restart
    assert 'if [[ "$API_ALREADY_DEPLOYED" == "true" ]]; then' in script[upgrade:restart]
    assert "kubectl get deployment api-staging" in script[check:upgrade]
