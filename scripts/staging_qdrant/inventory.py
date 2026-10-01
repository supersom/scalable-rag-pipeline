"""Classify the complete EBS volume inventory without making changes."""

from dataclasses import dataclass
import re
from typing import Literal


PVC_NAME = re.compile(r"^qdrant-storage-qdrant-([012])$")
PVC_PREFIX = "qdrant-storage-qdrant-"
PVC_TAG = "kubernetes.io/created-for/pvc/name"
NAMESPACE_TAG = "kubernetes.io/created-for/pvc/namespace"
CLUSTER_TAG = "ebs.csi.aws.com/cluster-name"


class UnsafeInventory(ValueError):
    """The observed volume set is not safe to mount or extend."""


@dataclass(frozen=True)
class Volume:
    ordinal: int
    volume_id: str
    state: str
    zone: str
    size_gib: int
    tags: dict[str, str]
    attachments: tuple[str, ...]


@dataclass(frozen=True)
class Inventory:
    kind: Literal["retained", "fresh", "resume_fresh"]
    volumes: tuple[Volume, ...]
    attempt_id: str | None = None


def tags_of(row: dict) -> dict[str, str]:
    tags: dict[str, str] = {}
    for tag in row.get("Tags", []):
        key, value = tag.get("Key"), tag.get("Value")
        if not isinstance(key, str) or not isinstance(value, str) or key in tags:
            raise UnsafeInventory("malformed or duplicate EBS tag")
        tags[key] = value
    return tags


def ordinal_of(row: dict, cluster: str) -> int | None:
    tags = tags_of(row)
    name = tags.get(PVC_TAG, "")
    if not name.startswith(PVC_PREFIX):
        return None
    match = PVC_NAME.fullmatch(name)
    if match is None:
        raise UnsafeInventory("malformed Qdrant ordinal tag")
    if tags.get(NAMESPACE_TAG) != "default" or tags.get(CLUSTER_TAG) != cluster:
        raise UnsafeInventory("Qdrant volume ownership mismatch")
    return int(match.group(1))


def classify(
    raw_pages: list[dict], cluster: str, *, allowed_attachment_ids: frozenset[str] = frozenset()
) -> Inventory:
    """Classify a fully paginated describe-volumes result by exact ordinal tags.

    The optional attachment allowlist is supplied only after Kubernetes binding
    checks establish that each instance belongs to the active Qdrant nodes.
    """
    if not raw_pages:
        raise UnsafeInventory("missing EBS inventory page")
    if any("Volumes" not in page or not isinstance(page["Volumes"], list) for page in raw_pages):
        raise UnsafeInventory("malformed EBS inventory page")
    if any(not page.get("NextToken") for page in raw_pages[:-1]) or raw_pages[-1].get("NextToken"):
        raise UnsafeInventory("incomplete EBS inventory page sequence")

    volumes: dict[int, Volume] = {}
    seen_ids: set[str] = set()
    for page in raw_pages:
        for row in page["Volumes"]:
            ordinal = ordinal_of(row, cluster)
            if ordinal is None:
                continue
            volume_id = row.get("VolumeId")
            if not isinstance(volume_id, str) or not volume_id or volume_id in seen_ids:
                raise UnsafeInventory("duplicate or missing EBS volume ID")
            if ordinal in volumes:
                raise UnsafeInventory("duplicate Qdrant ordinal")
            seen_ids.add(volume_id)
            attachments = tuple(a.get("InstanceId", "") for a in row.get("Attachments", []))
            if any(not instance or instance not in allowed_attachment_ids for instance in attachments):
                raise UnsafeInventory("Qdrant volume attached to an unexpected instance")
            if row.get("AvailabilityZone") != "us-east-1b" or row.get("Size") != 50:
                raise UnsafeInventory("Qdrant volume has unexpected size or zone")
            state = row.get("State")
            if state not in {"available", "in-use"} or (state == "in-use") != bool(attachments):
                raise UnsafeInventory("Qdrant volume has unsafe EBS state")
            volumes[ordinal] = Volume(
                ordinal=ordinal,
                volume_id=volume_id,
                state=state,
                zone=row["AvailabilityZone"],
                size_gib=row["Size"],
                tags=tags_of(row),
                attachments=attachments,
            )

    if not volumes:
        return Inventory("fresh", ())
    ordered = tuple(volumes[i] for i in sorted(volumes))
    origins = {v.tags.get("qdrant-volume-origin", "retained") for v in ordered}
    if origins == {"retained"} and set(volumes) == {0, 1, 2}:
        return Inventory("retained", ordered)
    if origins == {"fresh"}:
        attempts = {v.tags.get("recovery-attempt") for v in ordered}
        if len(attempts) == 1 and None not in attempts and "" not in attempts:
            return Inventory("resume_fresh", ordered, attempts.pop())
    raise UnsafeInventory("partial, mixed, or ambiguous Qdrant volume set")
