"""
Batch ingestion loop — runs independently in the background.
Processes eval/datasets/noisy_data/ in batches of 20, validates each batch
against Qdrant, moves successes to eval/datasets/ingested/, failures to
eval/datasets/failed/.

Flags:
  --graph-only   Skip vector embedding; run graph extraction on already-ingested
                 files in eval/datasets/ingested/ (first 10 chunks per file).
"""
import argparse
import os, sys, time, subprocess, shutil, logging, json
from pathlib import Path
import urllib.request
import ray

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/tmp/batch_ingest.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ROOT         = Path("/home/som/code/scalable-rag-pipeline")
NOISY_DIR    = ROOT / "eval/datasets/noisy_data"
INGESTED_DIR = ROOT / "eval/datasets/ingested"
DIGESTED_DIR = ROOT / "eval/datasets/digested"
FAILED_DIR   = ROOT / "eval/datasets/failed"
STAGE_DIR    = Path("/tmp/batch_stage")
BATCH_SIZE   = 20
QDRANT_URL   = "http://localhost:6333"
COLLECTION   = "rag_collection"
ENV_FILE     = ROOT / ".env"
MAX_RETRIES  = 2


def qdrant_count():
    url = f"{QDRANT_URL}/collections/{COLLECTION}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())["result"]["vectors_count"]


def qdrant_filenames():
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
        data=json.dumps({"limit": 50000, "with_payload": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        points = json.loads(r.read())["result"]["points"]
    return {
        p["payload"].get("metadata", {}).get("filename")
        or p["payload"].get("filename", "")
        for p in points
    }


def load_env():
    env = os.environ.copy()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def run_pipeline(source: Path, env: dict) -> bool:
    cmd = [
        sys.executable, "pipelines/ingestion/main.py",
        str(source), "--no-graph", "--no-init-ray"
    ]
    result = subprocess.run(
        cmd, cwd=str(ROOT), env=env,
        capture_output=False,
        timeout=3600,
    )
    return result.returncode == 0


def run_graph_pipeline(source: Path, env: dict) -> bool:
    cmd = [
        sys.executable, "scripts/ingest_local.py",
        str(source), "--graph-only"
    ]
    result = subprocess.run(
        cmd, cwd=str(ROOT), env=env,
        capture_output=False,
        timeout=7200,
    )
    return result.returncode == 0


def stage_batch(files: list[Path]):
    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    STAGE_DIR.mkdir(parents=True)
    for f in files:
        (STAGE_DIR / f.name).symlink_to(f.resolve())


def ingest_and_verify(files: list[Path], env: dict) -> bool:
    basenames = {f.name for f in files}
    before = qdrant_count()
    log.info(f"Qdrant before: {before} chunks")

    stage_batch(files)
    log.info(f"Running pipeline on {len(files)} files...")
    run_pipeline(STAGE_DIR, env)

    after = qdrant_count()
    log.info(f"Qdrant after: {after} chunks (delta: {after - before})")

    # Success if count grew OR any file from batch appears in payloads
    if after > before:
        return True
    known = qdrant_filenames()
    if basenames & known:
        return True
    log.warning("No new chunks detected — batch may have failed")
    return False


def move_files(files: list[Path], dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.move(str(f), dest / f.name)
    log.info(f"Moved {len(files)} files → {dest}")


# ── Args ─────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Batch ingestion loop")
parser.add_argument("--graph-only", action="store_true",
                    help="Run graph extraction on already-ingested files; skip embedding")
args = parser.parse_args()

if args.graph_only:
    env = load_env()
    env["PYTHONPATH"] = str(ROOT)
    files = sorted(INGESTED_DIR.glob("*"))
    files = [f for f in files if f.is_file()]
    if not files:
        log.info("eval/datasets/ingested/ is empty — nothing to graph-ingest.")
        sys.exit(0)
    log.info(f"Graph-only mode: {len(files)} files from {INGESTED_DIR}")
    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        stage_batch(batch)
        log.info(f"Graph batch {batch_num}: {len(batch)} files")
        success = run_graph_pipeline(STAGE_DIR, env)
        if success:
            move_files(batch, DIGESTED_DIR)
        else:
            log.warning(f"Graph batch {batch_num} failed — files remain in ingested/")
    log.info("Graph-only ingestion complete.")
    sys.exit(0)

# ── Start a persistent Ray cluster for all batches ───────────────────────────
env = load_env()
env["PYTHONPATH"] = str(ROOT)

worker_env = {k: env[k] for k in (
    "QDRANT_HOST", "QDRANT_PORT", "QDRANT_COLLECTION",
    "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
    "RAY_EMBED_ENDPOINT", "EMBED_MODEL_NAME",
    "RAY_LLM_ENDPOINT", "LLM_MODEL_NAME",
) if k in env}

if not ray.is_initialized():
    try:
        ray.init(address="auto", runtime_env={"env_vars": worker_env})
        log.info("Attached to existing Ray cluster.")
    except Exception:
        ray.init(runtime_env={"env_vars": worker_env})
        log.info("Started persistent Ray cluster for batch loop.")

# Pass the Ray address so subprocesses connect instead of starting fresh
env["RAY_ADDRESS"] = ray.get_runtime_context().gcs_address

while True:
    remaining = sorted(NOISY_DIR.glob("*"), key=lambda p: p.name)
    remaining = [f for f in remaining if f.is_file()]
    if not remaining:
        log.info("eval/datasets/noisy_data/ is empty — all done.")
        break

    batch = remaining[:BATCH_SIZE]
    log.info(f"--- Batch: {len(batch)} files ({len(remaining)} remaining) ---")
    for f in batch:
        log.info(f"  {f.name}")

    success = False
    for attempt in range(1, MAX_RETRIES + 1):
        log.info(f"Attempt {attempt}/{MAX_RETRIES}")
        try:
            success = ingest_and_verify(batch, env)
        except Exception as e:
            log.error(f"Pipeline error on attempt {attempt}: {e}")
            success = False
        if success:
            break
        if attempt < MAX_RETRIES:
            log.info("Retrying in 10s...")
            time.sleep(10)

    if success:
        move_files(batch, INGESTED_DIR)
    else:
        log.warning(f"Batch failed after {MAX_RETRIES} attempts — moving to failed/")
        move_files(batch, FAILED_DIR)

ray.shutdown()
log.info("Batch ingestion loop complete.")
remaining_count = len(list(NOISY_DIR.glob("*")))
log.info(f"Files left in noisy_data/: {remaining_count}")
log.info(f"Total Qdrant chunks: {qdrant_count()}")
