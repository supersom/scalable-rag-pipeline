"""Fail-closed discovery of the three Qdrant disks before any AWS mutation."""

import pytest

from scripts.staging_qdrant.inventory import UnsafeInventory, classify


CLUSTER = "rag-platform-cluster"


def old_volume(ordinal, **changes):
    row = {
        "VolumeId": f"vol-{ordinal}",
        "State": "available",
        "AvailabilityZone": "us-east-1b",
        "Size": 50,
        "Attachments": [],
        "Tags": [
            {"Key": "kubernetes.io/created-for/pvc/name", "Value": f"qdrant-storage-qdrant-{ordinal}"},
            {"Key": "kubernetes.io/created-for/pvc/namespace", "Value": "default"},
            {"Key": "ebs.csi.aws.com/cluster-name", "Value": CLUSTER},
        ],
    }
    row.update(changes)
    return row


def replace_tag(row, key, value):
    row["Tags"] = [
        {"Key": tag["Key"], "Value": value if tag["Key"] == key else tag["Value"]}
        for tag in row["Tags"]
    ]
    return row


def test_exact_three_old_volumes_map_by_ordinal_not_list_order():
    rows = [old_volume(2), old_volume(0), old_volume(1)]
    inventory = classify([{"Volumes": rows}], CLUSTER)
    assert inventory.kind == "retained"
    assert [volume.ordinal for volume in inventory.volumes] == [0, 1, 2]
    assert [volume.volume_id for volume in inventory.volumes] == ["vol-0", "vol-1", "vol-2"]


@pytest.mark.parametrize("ordinals", [(0, 1), (0, 0, 1, 2)])
def test_partial_or_duplicate_old_set_fails_closed(ordinals):
    with pytest.raises(UnsafeInventory):
        classify([{"Volumes": [old_volume(i) for i in ordinals]}], CLUSTER)


def test_second_page_duplicate_is_not_ignored():
    pages = [
        {"Volumes": [old_volume(0), old_volume(1), old_volume(2)], "NextToken": "next"},
        {"Volumes": [old_volume(2)]},
    ]
    with pytest.raises(UnsafeInventory):
        classify(pages, CLUSTER)


def test_incomplete_pagination_fails_closed():
    with pytest.raises(UnsafeInventory, match="page"):
        classify([{"Volumes": [old_volume(i) for i in range(3)], "NextToken": "missing"}], CLUSTER)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("kubernetes.io/created-for/pvc/namespace", "other"),
        ("ebs.csi.aws.com/cluster-name", "other-cluster"),
    ],
)
def test_matching_ordinal_with_wrong_owner_is_not_ignored(key, value):
    rows = [old_volume(i) for i in range(3)]
    replace_tag(rows[1], key, value)
    with pytest.raises(UnsafeInventory, match="ownership"):
        classify([{"Volumes": rows}], CLUSTER)


@pytest.mark.parametrize("change", [{"AvailabilityZone": "us-east-1a"}, {"Size": 40}])
def test_wrong_zone_or_size_fails_closed(change):
    rows = [old_volume(i) for i in range(3)]
    rows[0].update(change)
    with pytest.raises(UnsafeInventory):
        classify([{"Volumes": rows}], CLUSTER)


def test_foreign_attachment_fails_closed():
    rows = [old_volume(i) for i in range(3)]
    rows[0]["Attachments"] = [{"InstanceId": "i-foreign"}]
    with pytest.raises(UnsafeInventory, match="attached"):
        classify([{"Volumes": rows}], CLUSTER)


def test_no_matching_volumes_is_fresh():
    assert classify([{"Volumes": []}], CLUSTER).kind == "fresh"


def test_one_script_owned_fresh_volume_can_resume_only_its_attempt():
    row = old_volume(0)
    row["Tags"].extend([
        {"Key": "qdrant-volume-origin", "Value": "fresh"},
        {"Key": "recovery-attempt", "Value": "attempt-1"},
    ])
    inventory = classify([{"Volumes": [row]}], CLUSTER)
    assert inventory.kind == "resume_fresh"
    assert inventory.attempt_id == "attempt-1"


def test_mixed_attempt_or_fresh_and_retained_is_unsafe():
    rows = [old_volume(0), old_volume(1)]
    for ordinal, row in enumerate(rows):
        row["Tags"].extend([
            {"Key": "qdrant-volume-origin", "Value": "fresh"},
            {"Key": "recovery-attempt", "Value": f"attempt-{ordinal}"},
        ])
    with pytest.raises(UnsafeInventory):
        classify([{"Volumes": rows}], CLUSTER)
    rows[1]["Tags"] = old_volume(1)["Tags"]
    with pytest.raises(UnsafeInventory):
        classify([{"Volumes": rows}], CLUSTER)
