from typing import Any

from services.aws_emulator.init_s3 import BUCKET, _client


def main(client: Any | None = None) -> None:
    if client is None:
        client = _client()
    status = client.get_bucket_versioning(Bucket=BUCKET).get("Status")
    if status != "Enabled":
        raise SystemExit(f"bucket {BUCKET!r} is not ready: versioning={status!r}")


if __name__ == "__main__":
    main()
