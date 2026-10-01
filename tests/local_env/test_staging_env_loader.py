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
    keys = ["EMBED_MODEL_NAME", "EMBED_DIM", "LLM_MODEL_NAME", "LLM_API_KEY"]
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
