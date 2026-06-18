#!/usr/bin/env python3
"""One-time script: download model weights from HuggingFace and upload to S3.

GPU node cold starts will then pull from S3 via the free Gateway VPC endpoint
instead of HuggingFace via NAT (~$0.90/node for 18 GB of weights at $0.045/GB).

Usage:
    pip install huggingface_hub
    python scripts/cache_models_s3.py
    python scripts/cache_models_s3.py --bucket my-custom-bucket
    python scripts/cache_models_s3.py --models BAAI/bge-m3   # subset

Re-running is safe: `aws s3 sync` skips files that already match in S3.
"""

import argparse
import subprocess
import sys

DEFAULT_BUCKET = "rag-platform-models-prod-7649"

MODELS = [
    "NousResearch/Meta-Llama-3-8B-Instruct",  # ~16 GB
    "BAAI/bge-m3",                              # ~2.2 GB
]


def sync_model(model_id: str, bucket: str) -> None:
    from huggingface_hub import snapshot_download

    print(f"\n{'='*60}")
    print(f"Model : {model_id}")
    print(f"Bucket: s3://{bucket}/models/{model_id}/")
    print(f"{'='*60}")

    print("Downloading from HuggingFace...")
    local_path = snapshot_download(model_id)
    print(f"Cached at: {local_path}")

    s3_path = f"s3://{bucket}/models/{model_id}/"
    print(f"Uploading to {s3_path} ...")
    subprocess.run(
        ["aws", "s3", "sync", local_path, s3_path, "--no-progress"],
        check=True,
    )
    print(f"Done: {model_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="Target S3 bucket")
    parser.add_argument("--models", nargs="+", default=MODELS, metavar="MODEL_ID",
                        help="HuggingFace model IDs to cache (default: all)")
    args = parser.parse_args()

    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print("ERROR: huggingface_hub not installed. Run: pip install huggingface_hub", file=sys.stderr)
        sys.exit(1)

    for model_id in args.models:
        sync_model(model_id, args.bucket)

    print("\nAll models cached. GPU nodes will now pull from S3 on cold start.")
    print(f"Verify: aws s3 ls s3://{args.bucket}/models/ --recursive --human-readable | head -20")


if __name__ == "__main__":
    main()
