"""Versioned payload storage against Moto (round trips) and Stubber (request shape)."""
import asyncio
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from services.api.app.evaluation.bounded_io import BoundedIO
from services.api.app.evaluation.delivery_models import ObjectVersion, PayloadSlot, RedactedPayload
from services.api.app.evaluation.payloads import (
    DisabledPayloadStore, PayloadAccessDenied, PayloadStorageError, S3PayloadStore,
)

# botocore 1.34 request signing calls datetime.utcnow(); that is not code under test.
pytestmark = pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning:botocore')

EVIDENCE_BUCKET = 'synthetic-evidence-payloads'
KMS_KEY = 'synthetic-dedicated-evidence-key'
POLICY = 'restricted-synthetic-v1'
TENANT = 'hmac-sha256:' + 'a' * 64
OTHER_TENANT = 'hmac-sha256:' + 'b' * 64
RUN_ID = UUID('22222222-2222-2222-2222-222222222222')
CREATED = datetime(2026, 1, 1, 23, 59, 30, tzinfo=timezone.utc)
DATA = b'{"text":"Contact [REDACTED_EMAIL]"}'


class Clocks:
    def __init__(self):
        self.now = CREATED

    def utc(self):
        return self.now

    @staticmethod
    def monotonic():
        return 0.0


class SdkSpy:
    """Delegating S3 client that counts the calls the store makes."""

    def __init__(self, client):
        self._client = client
        self.get_calls = self.put_calls = self.list_calls = self.delete_calls = 0

    def get_object(self, **kwargs):
        self.get_calls += 1
        return self._client.get_object(**kwargs)

    def put_object(self, **kwargs):
        self.put_calls += 1
        return self._client.put_object(**kwargs)

    def list_object_versions(self, **kwargs):
        self.list_calls += 1
        return self._client.list_object_versions(**kwargs)

    def delete_objects(self, **kwargs):
        self.delete_calls += 1
        return self._client.delete_objects(**kwargs)

    def close(self):
        pass


def payload(kind='input', ordinal=1, item=None, data=DATA, policy=POLICY):
    return RedactedPayload(slot=PayloadSlot(kind=kind, ordinal=ordinal, item_ordinal=item),
                           data=data, policy_version=policy)


def make_store(client, clocks, **kwargs):
    return S3PayloadStore(client, bucket=EVIDENCE_BUCKET, kms_key_id=KMS_KEY, environment='test',
                          io=BoundedIO(2, clock=clocks.monotonic), clock=clocks.utc,
                          monotonic=clocks.monotonic, policy_versions=(POLICY,), **kwargs)


@pytest.fixture
def evidence_bucket():
    return EVIDENCE_BUCKET


@pytest.fixture
def clocks():
    return Clocks()


@pytest.fixture
def sdk_spy(s3_client):
    return SdkSpy(s3_client)


@pytest.fixture
def store(sdk_spy, clocks):
    store = make_store(sdk_spy, clocks)
    yield store
    assert asyncio.run(store.close(deadline=clocks.monotonic() + 5)) == 0


@pytest.fixture
def stored_ref(store, sdk_spy):
    ref = asyncio.run(store.put(RUN_ID, TENANT, payload(), deadline=100.0))
    assert sdk_spy.put_calls == 1
    return ref


def test_round_trip_returns_exact_bytes_and_reference(store, stored_ref, s3_client):
    assert stored_ref.bucket == EVIDENCE_BUCKET
    assert stored_ref.tenant_partition == TENANT
    assert stored_ref.slot == PayloadSlot(kind='input', ordinal=1)
    assert stored_ref.byte_size == len(DATA)
    assert stored_ref.sha256 == hashlib.sha256(DATA).hexdigest()
    assert stored_ref.policy_version == POLICY
    assert stored_ref.created_at == CREATED
    assert stored_ref.expires_at == CREATED + timedelta(days=30)
    head = s3_client.head_object(Bucket=EVIDENCE_BUCKET, Key=stored_ref.key, VersionId=stored_ref.version_id)
    assert head['ServerSideEncryption'] == 'aws:kms'
    assert asyncio.run(store.get(stored_ref, TENANT, deadline=100.0)) == DATA


def test_generated_key_namespace(store):
    ref = asyncio.run(store.put(RUN_ID, TENANT, payload('retrieval_chunk', 2, 3), deadline=100.0))
    namespace = store.namespace(TENANT, RUN_ID)
    assert namespace == f'evidence/test/{TENANT}/{RUN_ID}/'
    assert ref.key == f'{namespace}2026-01-01/retrieval_chunk/2/3'
    model = asyncio.run(store.put(RUN_ID, TENANT, payload('model_request', 4), deadline=100.0))
    assert model.key == f'{namespace}2026-01-01/model_request/4'


def test_generic_operation_content_round_trip_uses_exact_version(store):
    candidate = payload('operation_content', 3, 2)
    ref = asyncio.run(store.put(RUN_ID, TENANT, candidate, deadline=100.0))
    assert ref.key.endswith('/2026-01-01/operation_content/3/2')
    assert asyncio.run(store.get(ref, TENANT, deadline=100.0)) == DATA


@pytest.mark.parametrize('tenant', ['', '../other', 'a/b', 'x' * 200, 'tenant with space', '.', 'a..b'])
def test_unsafe_tenant_partition_rejected_before_io(store, sdk_spy, tenant):
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.put(RUN_ID, tenant, payload(), deadline=100.0))
    assert sdk_spy.put_calls == 0


def test_retry_creates_new_version_without_changing_existing_reference(store, stored_ref):
    second = payload(data=b'{"text":"second attempt"}')
    retried = asyncio.run(store.put(RUN_ID, TENANT, second, deadline=100.0))
    assert retried.key == stored_ref.key
    assert retried.version_id != stored_ref.version_id
    assert asyncio.run(store.get(stored_ref, TENANT, deadline=100.0)) == DATA
    assert asyncio.run(store.get(retried, TENANT, deadline=100.0)) == second.data


def test_wrong_tenant_is_denied_before_io(store, stored_ref, sdk_spy):
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.get(stored_ref, 'tenant-b', deadline=100.0))
    assert sdk_spy.get_calls == 0


@pytest.mark.parametrize('tenant', [None, '', OTHER_TENANT])
def test_missing_or_other_tenant_is_denied_before_io(store, stored_ref, sdk_spy, tenant):
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.get(stored_ref, tenant, deadline=100.0))
    assert sdk_spy.get_calls == 0


def _manipulations(ref):
    base = ref.key
    namespace = f'evidence/test/{TENANT}/{RUN_ID}/'
    return {
        'bucket': {'bucket': 'another-bucket'},
        'tenant_key': {'key': base.replace(TENANT, OTHER_TENANT)},
        'environment': {'key': base.replace('evidence/test/', 'evidence/prod/')},
        'root': {'key': base.replace('evidence/', 'other/', 1)},
        'traversal': {'key': f'{namespace}2026-01-01/../../{RUN_ID}/2026-01-01/input/1'},
        'slot': {'key': f'{namespace}2026-01-01/output/1'},
        'date': {'key': f'{namespace}2026-01-02/input/1'},
        'run_id': {'key': base.replace(str(RUN_ID), 'not-a-uuid')},
        'extra_segment': {'key': base + '/extra'},
        'policy': {'policy_version': 'unreviewed-policy'},
        'expiry_extended': {'expires_at': ref.expires_at + timedelta(days=1)},
        'oversized': {'byte_size': 65537},
    }


@pytest.mark.parametrize('name', ['bucket', 'tenant_key', 'environment', 'root', 'traversal',
                                  'slot', 'date', 'run_id', 'extra_segment', 'policy', 'expiry_extended',
                                  'oversized'])
def test_manipulated_reference_is_denied_before_io(store, stored_ref, sdk_spy, name):
    forged = stored_ref.model_copy(update=_manipulations(stored_ref)[name])
    authorized = forged.tenant_partition
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.get(forged, authorized, deadline=100.0))
    assert sdk_spy.get_calls == 0


def test_manipulated_version_is_not_resolved(store, stored_ref):
    forged = stored_ref.model_copy(update={'version_id': 'forged-version'})
    with pytest.raises(PayloadStorageError):
        asyncio.run(store.get(forged, TENANT, deadline=100.0))


@pytest.mark.parametrize('update', [{'sha256': 'f' * 64}, {'byte_size': len(DATA) + 1}, {'byte_size': len(DATA) - 1}])
def test_hash_or_size_mismatch_is_rejected_after_read(store, stored_ref, sdk_spy, update):
    forged = stored_ref.model_copy(update=update)
    with pytest.raises(PayloadStorageError) as caught:
        asyncio.run(store.get(forged, TENANT, deadline=100.0))
    assert caught.value.reason == 'integrity'
    assert sdk_spy.get_calls == 1


def test_expired_reference_is_denied_before_io(store, stored_ref, sdk_spy, clocks):
    clocks.now = stored_ref.expires_at - timedelta(microseconds=1)
    assert asyncio.run(store.get(stored_ref, TENANT, deadline=100.0)) == DATA
    clocks.now = stored_ref.expires_at
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.get(stored_ref, TENANT, deadline=100.0))
    assert sdk_spy.get_calls == 1


def test_put_rejects_unreviewed_policy_and_oversized_payload_before_io(store, sdk_spy):
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.put(RUN_ID, TENANT, payload(policy='unreviewed-policy'), deadline=100.0))
    with pytest.raises(PayloadStorageError) as caught:
        asyncio.run(store.put(RUN_ID, TENANT, payload(data=b'x' * 65537), deadline=100.0))
    assert caught.value.reason == 'too_large'
    assert sdk_spy.put_calls == 0


def test_deletes_all_versions_and_delete_markers(store, s3_client):
    first = asyncio.run(store.put(RUN_ID, TENANT, payload(), deadline=100.0))
    asyncio.run(store.put(RUN_ID, TENANT, payload(data=b'{"text":"v2"}'), deadline=100.0))
    s3_client.delete_object(Bucket=EVIDENCE_BUCKET, Key=first.key)
    other = asyncio.run(store.put(UUID(int=7), TENANT, payload(), deadline=100.0))

    namespace = store.namespace(TENANT, RUN_ID)
    versions = asyncio.run(store.list_versions(namespace, deadline=100.0))
    assert len(versions) == 3
    assert sum(v.is_delete_marker for v in versions) == 1
    assert all(v.key == first.key for v in versions)

    assert asyncio.run(store.delete_versions(versions, deadline=100.0)) == 3
    assert asyncio.run(store.list_versions(namespace, deadline=100.0)) == ()
    remaining = s3_client.list_object_versions(Bucket=EVIDENCE_BUCKET)
    assert [v['VersionId'] for v in remaining['Versions']] == [other.version_id]
    assert 'DeleteMarkers' not in remaining
    assert asyncio.run(store.delete_versions(versions, deadline=100.0)) == 3  # Idempotent.
    assert asyncio.run(store.delete_versions((), deadline=100.0)) == 0


def test_list_versions_paginates(sdk_spy, clocks):
    store = make_store(sdk_spy, clocks, list_page_size=2)
    try:
        for n in range(5):
            asyncio.run(store.put(RUN_ID, TENANT, payload(data=f'{{"n":{n}}}'.encode()), deadline=100.0))
        versions = asyncio.run(store.list_versions(store.namespace(TENANT, RUN_ID), deadline=100.0))
        assert len({v.version_id for v in versions}) == 5
        assert sdk_spy.list_calls == 3
    finally:
        asyncio.run(store.close(deadline=5.0))


@pytest.mark.parametrize('prefix', ['', 'evidence/', 'evidence/prod/', 'evidence/test/../x/', 'other/test/',
                                    f'evidence/test/{TENANT}/..//'])
def test_list_outside_configured_namespace_is_denied_before_io(store, sdk_spy, prefix):
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.list_versions(prefix, deadline=100.0))
    assert sdk_spy.list_calls == 0


def test_delete_outside_configured_namespace_is_denied_before_io(store, sdk_spy):
    versions = (ObjectVersion(key=f'evidence/test/{TENANT}/{RUN_ID}/2026-01-01/input/1', version_id='v1'),
                ObjectVersion(key='chat-history/other-object', version_id='v2'))
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.delete_versions(versions, deadline=100.0))
    assert sdk_spy.delete_calls == 0


def test_partial_delete_failure_is_counted_not_hidden(clocks):
    class FailingDelete:
        def delete_objects(self, **kwargs):
            objects = kwargs['Delete']['Objects']
            return {'Deleted': objects[:1], 'Errors': [{'Key': o['Key'], 'VersionId': o['VersionId'],
                                                        'Code': 'AccessDenied'} for o in objects[1:]]}

        def close(self):
            pass
    store = make_store(FailingDelete(), clocks)
    key = f'evidence/test/{TENANT}/{RUN_ID}/2026-01-01/input/1'
    versions = (ObjectVersion(key=key, version_id='v1'), ObjectVersion(key=key, version_id='v2'))
    try:
        assert asyncio.run(store.delete_versions(versions, deadline=100.0)) == 1
    finally:
        asyncio.run(store.close(deadline=5.0))


# Stubber tests pin the exact production request shape without any emulator.

@pytest.fixture
def stubbed(aws_env, clocks):
    import boto3
    from botocore.stub import Stubber
    client = boto3.client('s3', region_name='us-east-1')
    stubber = Stubber(client)
    stubber.activate()
    store = make_store(client, clocks)
    yield store, stubber
    asyncio.run(store.close(deadline=5.0))
    stubber.deactivate()


def _expected_put():
    return {'Bucket': EVIDENCE_BUCKET, 'Key': f'evidence/test/{TENANT}/{RUN_ID}/2026-01-01/input/1', 'Body': DATA,
            'ContentType': 'application/json', 'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': KMS_KEY}


def test_put_requests_dedicated_kms_key(stubbed):
    store, stubber = stubbed
    stubber.add_response('put_object', {'VersionId': 'exact-version', 'ServerSideEncryption': 'aws:kms'},
                         _expected_put())
    ref = asyncio.run(store.put(RUN_ID, TENANT, payload(), deadline=100.0))
    stubber.assert_no_pending_responses()
    assert ref.version_id == 'exact-version'


@pytest.mark.parametrize('response', [{}, {'VersionId': '', 'ServerSideEncryption': 'aws:kms'},
                                      {'VersionId': 'v1'}, {'VersionId': 'v1', 'ServerSideEncryption': 'AES256'}])
def test_missing_version_or_encryption_yields_no_reference(stubbed, response):
    store, stubber = stubbed
    stubber.add_response('put_object', response, _expected_put())
    with pytest.raises(PayloadStorageError) as caught:
        asyncio.run(store.put(RUN_ID, TENANT, payload(), deadline=100.0))
    assert caught.value.reason == 'failed'


def test_failed_upload_is_content_free_and_yields_no_reference(stubbed):
    store, stubber = stubbed
    stubber.add_client_error('put_object', service_error_code='AccessDenied',
                             service_message=f'denied {TENANT} REDACTED_EMAIL', http_status_code=403)
    with pytest.raises(PayloadStorageError) as caught:
        asyncio.run(store.put(RUN_ID, TENANT, payload(), deadline=100.0))
    error = caught.value
    assert error.reason == 'failed'
    for unsafe in (TENANT, 'REDACTED_EMAIL', EVIDENCE_BUCKET, str(RUN_ID), 'AccessDenied'):
        assert unsafe not in str(error) and unsafe not in repr(error)
    assert error.__cause__ is None and error.__suppress_context__


def test_get_requests_exact_version(stubbed):
    store, stubber = stubbed
    from io import BytesIO
    from botocore.response import StreamingBody
    stubber.add_response('put_object', {'VersionId': 'exact-version', 'ServerSideEncryption': 'aws:kms'},
                         _expected_put())
    ref = asyncio.run(store.put(RUN_ID, TENANT, payload(), deadline=100.0))
    stubber.add_response('get_object', {'VersionId': 'exact-version', 'ContentLength': len(DATA),
                                        'Body': StreamingBody(BytesIO(DATA), len(DATA))},
                         {'Bucket': EVIDENCE_BUCKET, 'Key': ref.key, 'VersionId': 'exact-version'})
    assert asyncio.run(store.get(ref, TENANT, deadline=100.0)) == DATA


def test_hung_sdk_call_is_bounded_by_deadline(aws_env):
    release = threading.Event()

    class HungClient:
        def put_object(self, **kwargs):
            release.wait(5)
            return {'VersionId': 'late', 'ServerSideEncryption': 'aws:kms'}

        def close(self):
            pass
    clocks = Clocks()
    store = S3PayloadStore(HungClient(), bucket=EVIDENCE_BUCKET, kms_key_id=KMS_KEY, environment='test',
                           io=BoundedIO(1), clock=clocks.utc, policy_versions=(POLICY,))
    try:
        started = time.monotonic()
        with pytest.raises(PayloadStorageError) as caught:
            asyncio.run(store.put(RUN_ID, TENANT, payload(), deadline=time.monotonic() + 0.2))
        assert caught.value.reason == 'timeout'
        assert time.monotonic() - started < 2
        assert asyncio.run(store.close(deadline=time.monotonic() + 0.05)) == 1
    finally:
        release.set()


def test_from_config_uses_finite_socket_timeouts_and_retries(aws_env):
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    config = DeliveryConfig(bucket=EVIDENCE_BUCKET, kms_key_id=KMS_KEY, environment='test',
                            s3_endpoint_url='http://127.0.0.1:5000', s3_workers=2)
    store = S3PayloadStore.from_config(config)
    try:
        client_config = store._client.meta.config
        assert client_config.connect_timeout == 1
        assert client_config.read_timeout == 2
        assert client_config.retries == {'total_max_attempts': 2, 'mode': 'standard'}
        assert store._io.workers == 2
    finally:
        asyncio.run(store.close(deadline=time.monotonic() + 1))


def test_from_config_requires_bucket_and_key(aws_env):
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    with pytest.raises(ValueError):
        S3PayloadStore.from_config(DeliveryConfig(kms_key_id=KMS_KEY))
    with pytest.raises(ValueError):
        S3PayloadStore.from_config(DeliveryConfig(bucket=EVIDENCE_BUCKET))


def test_disabled_store_allocates_nothing_and_stores_nothing(stored_ref):
    store = DisabledPayloadStore()
    with pytest.raises(PayloadStorageError) as caught:
        asyncio.run(store.put(RUN_ID, TENANT, payload(), deadline=100.0))
    assert caught.value.reason == 'disabled'
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.get(stored_ref, TENANT, deadline=100.0))
    assert asyncio.run(store.list_versions('evidence/test/', deadline=100.0)) == ()
    assert asyncio.run(store.delete_versions((), deadline=100.0)) == 0
    assert asyncio.run(store.close(deadline=100.0)) == 0
