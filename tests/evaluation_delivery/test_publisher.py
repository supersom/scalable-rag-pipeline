"""SQS publication of committed metadata envelopes against Moto (wire shape) and Stubber (failures)."""
import asyncio
import json
import logging
import threading
import time
from datetime import timedelta

import pytest
from botocore.exceptions import (
    ClientError, EndpointConnectionError, NoCredentialsError, NoRegionError, ParamValidationError,
    PartialCredentialsError,
)
from botocore.stub import Stubber

from services.api.app.evaluation.bounded_io import BoundedIO
from services.api.app.evaluation.delivery_config import DeliveryConfig
from services.api.app.evaluation.delivery_models import (
    DeliveryReason, ExportEnvelope, ManifestEntry, PayloadKind, PayloadRef, PayloadSlot, PublishReceipt,
)
from services.api.app.evaluation.publisher import PublishError, RunEvidencePublisher, parse_envelope

# botocore 1.34 request signing calls datetime.utcnow(); that is not code under test.
pytestmark = pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning:botocore')

QUEUE = 'synthetic-eval-runs'
SENTINEL = 'SENTINEL-QUEUE-SECRET-4242'
IDENTITY_ATTRIBUTES = {'export_version', 'run_schema_version', 'run_id', 'export_hash'}


@pytest.fixture
def sqs(aws_env):
    import boto3
    from moto import mock_aws
    with mock_aws():
        client = boto3.client('sqs', region_name='us-east-1')
        url = client.create_queue(QueueName=QUEUE)['QueueUrl']
        try:
            yield client, url
        finally:
            client.close()


def make_publisher(client, url, **kwargs):
    kwargs.setdefault('max_body_bytes', DeliveryConfig().queue_body_bytes)
    return RunEvidencePublisher(client, queue_url=url, io=BoundedIO(2), **kwargs)


def publish(publisher, data, *, timeout=5.0):
    return asyncio.run(publisher.publish(data, deadline=time.monotonic() + timeout))


def receive_all(client, url):
    response = client.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=0,
                                      MessageAttributeNames=['All'], VisibilityTimeout=300)
    return response.get('Messages', [])


def with_stored_reference(envelope):
    created = envelope.safe_run.completed_at
    slot = PayloadSlot(kind=PayloadKind.input, ordinal=1)
    ref = PayloadRef(bucket='synthetic-evidence-payloads',
                     key=f'evidence/test/synthetic-tenant/{envelope.run_id}/2026-01-01/input/1', version_id='v1',
                     tenant_partition='synthetic-tenant', slot=slot, byte_size=10, sha256='a' * 64,
                     policy_version=envelope.redaction_policy_version, created_at=created,
                     expires_at=created + timedelta(days=30))
    return ExportEnvelope(run_id=envelope.run_id, safe_run=envelope.safe_run,
                          redaction_policy_version=envelope.redaction_policy_version,
                          manifest=(ManifestEntry(slot=slot, state='stored', reference=ref),), completeness='complete')


def assert_safe(error: BaseException, reason, *, permanent):
    assert isinstance(error, PublishError)
    assert error.reason == reason and error.permanent is permanent
    assert SENTINEL not in str(error) and SENTINEL not in repr(error)
    assert error.__cause__ is None and error.__context__ is None


class CountingClient:
    """Delegating SQS client that counts sends; validation failures must never reach it."""

    def __init__(self, client=None):
        self._client = client
        self.sends = 0
        self.closed = False

    def send_message(self, **kwargs):
        self.sends += 1
        return self._client.send_message(**kwargs)

    def close(self):
        self.closed = True


# --- exact wire shape ------------------------------------------------------------

def test_body_is_exactly_the_canonical_envelope(sqs, envelope):
    client, url = sqs
    data = with_stored_reference(envelope).canonical_bytes()
    receipt = publish(make_publisher(client, url), data)
    [message] = receive_all(client, url)
    assert isinstance(receipt, PublishReceipt)
    assert receipt.acknowledgement_id == message['MessageId']
    # Nothing prepended, appended, re-serialized, or wrapped.
    assert message['Body'].encode('utf-8') == data
    attributes = {name: value['StringValue'] for name, value in message['MessageAttributes'].items()}
    assert attributes == {'export_version': '1.0', 'run_schema_version': '1.0', 'run_id': str(envelope.run_id),
                          'export_hash': envelope_hash(data)}


def envelope_hash(data):
    return json.loads(data)['export_hash']


def test_body_carries_references_but_never_payload_content(sqs, envelope):
    client, url = sqs
    data = with_stored_reference(envelope).canonical_bytes()
    publish(make_publisher(client, url), data)
    [message] = receive_all(client, url)
    body = json.loads(message['Body'])
    [entry] = body['manifest']
    assert set(entry['reference']) == {'bucket', 'key', 'version_id', 'tenant_partition', 'slot', 'byte_size',
                                       'sha256', 'policy_version', 'created_at', 'expires_at'}
    assert 'data' not in json.dumps(body['manifest'])
    assert set(message['MessageAttributes']) == IDENTITY_ATTRIBUTES


def test_identical_bytes_publish_identically_twice(sqs, envelope):
    client, url = sqs
    publisher = make_publisher(client, url)
    data = envelope.canonical_bytes()
    first, second = publish(publisher, data), publish(publisher, data)
    bodies = [m['Body'].encode('utf-8') for m in receive_all(client, url)]
    assert bodies == [data, data] and first.acknowledgement_id != second.acknowledgement_id


# --- validation before any SDK call ------------------------------------------------

def tampered_hash(envelope):
    value = json.loads(envelope.canonical_bytes())
    value['export_hash'] = '0' * 64
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def without_hash(envelope):
    return envelope.canonical_bytes(exclude_hash=True)


def reordered(envelope):
    return json.dumps(json.loads(envelope.canonical_bytes()), indent=1).encode()


def mutated_content(envelope):
    value = json.loads(envelope.canonical_bytes())
    value['safe_run']['error_class'] = SENTINEL
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


@pytest.mark.parametrize('build', [
    lambda e: b'',
    lambda e: b'\xff\xfe' + SENTINEL.encode(),
    lambda e: SENTINEL.encode(),
    lambda e: b'[]',
    tampered_hash,
    without_hash,
    reordered,
    mutated_content,
    lambda e: e.canonical_bytes() + b' ',
    lambda e: e.canonical_bytes().replace(b'"export_version":"1.0"', b'"export_version":"2.0"'),
], ids=['empty', 'not-utf8', 'not-json', 'wrong-shape', 'tampered-hash', 'missing-hash', 'non-canonical',
        'content-changed-after-hash', 'trailing-bytes', 'unknown-version'])
def test_invalid_envelopes_are_permanent_and_never_sent(envelope, build):
    client = CountingClient()
    publisher = make_publisher(client, 'https://sqs.us-east-1.amazonaws.com/1/q')
    with pytest.raises(PublishError) as caught:
        publish(publisher, build(envelope))
    assert_safe(caught.value, DeliveryReason.metadata_invalid, permanent=True)
    assert client.sends == 0


def test_characters_sqs_cannot_carry_are_rejected_before_send(envelope):
    value = json.loads(envelope.canonical_bytes(exclude_hash=True))
    value['safe_run']['error_class'] = 'bad￾class'
    tricky = ExportEnvelope.model_validate(value)
    client = CountingClient()
    publisher = make_publisher(client, 'https://sqs.us-east-1.amazonaws.com/1/q')
    with pytest.raises(PublishError) as caught:
        publish(publisher, tricky.canonical_bytes())
    assert_safe(caught.value, DeliveryReason.metadata_invalid, permanent=True)
    assert client.sends == 0


def test_body_cap_is_enforced_on_encoded_bytes(sqs, envelope):
    client, url = sqs
    data = envelope.canonical_bytes()
    counting = CountingClient(client)
    exact = RunEvidencePublisher(counting, queue_url=url, io=BoundedIO(1), max_body_bytes=len(data))
    publish(exact, data)
    over = RunEvidencePublisher(counting, queue_url=url, io=BoundedIO(1), max_body_bytes=len(data) - 1)
    with pytest.raises(PublishError) as caught:
        publish(over, data)
    assert_safe(caught.value, DeliveryReason.metadata_too_large, permanent=True)
    assert counting.sends == 1


def test_parse_envelope_returns_the_validated_identity(envelope):
    parsed = parse_envelope(envelope.canonical_bytes())
    assert parsed.run_id == envelope.run_id and parsed.export_hash == envelope.export_hash


# --- broker failures are classified without their messages ------------------------------

def stubbed(code, *, status=400):
    import boto3
    client = boto3.client('sqs', region_name='us-east-1')
    stubber = Stubber(client)
    stubber.add_client_error('send_message', service_error_code=code, service_message=f'echo {SENTINEL}',
                             http_status_code=status)
    stubber.activate()
    return client


@pytest.mark.parametrize('code', [
    'AccessDenied', 'AccessDeniedException', 'AWS.SimpleQueueService.NonExistentQueue', 'QueueDoesNotExist',
    'InvalidMessageContents', 'InvalidParameterValue', 'InvalidClientTokenId', 'UnrecognizedClientException',
    'SignatureDoesNotMatch', 'KmsAccessDenied', 'KmsDisabled', 'KmsNotFound', 'KmsInvalidState',
    'KmsInvalidKeyUsage', 'UnsupportedOperation', 'InvalidAddress', 'InvalidSecurity',
])
def test_denied_broker_errors_are_permanent(aws_env, envelope, code):
    publisher = make_publisher(stubbed(code), 'https://sqs.us-east-1.amazonaws.com/1/q')
    with pytest.raises(PublishError) as caught:
        publish(publisher, envelope.canonical_bytes())
    assert_safe(caught.value, DeliveryReason.broker_denied, permanent=True)


@pytest.mark.parametrize('code,status', [
    ('ThrottlingException', 400), ('RequestThrottled', 403), ('KmsThrottled', 400), ('ServiceUnavailable', 503),
    ('InternalError', 500), ('ExpiredToken', 400), ('RequestExpired', 400), ('SomethingNew', 400),
])
def test_other_broker_errors_are_transient(aws_env, envelope, code, status):
    publisher = make_publisher(stubbed(code, status=status), 'https://sqs.us-east-1.amazonaws.com/1/q')
    with pytest.raises(PublishError) as caught:
        publish(publisher, envelope.canonical_bytes())
    assert_safe(caught.value, DeliveryReason.broker_transient, permanent=False)


def test_missing_queue_in_moto_is_denied(sqs, envelope):
    client, url = sqs
    publisher = make_publisher(client, url.rsplit('/', 1)[0] + '/missing-queue')
    with pytest.raises(PublishError) as caught:
        publish(publisher, envelope.canonical_bytes())
    assert_safe(caught.value, DeliveryReason.broker_denied, permanent=True)


class RaisingClient:
    def __init__(self, error):
        self.error = error

    def send_message(self, **kwargs):
        raise self.error


@pytest.mark.parametrize('error,reason,permanent', [
    (EndpointConnectionError(endpoint_url=f'http://{SENTINEL}'), DeliveryReason.broker_transient, False),
    (ConnectionResetError(SENTINEL), DeliveryReason.broker_transient, False),
    (RuntimeError(SENTINEL), DeliveryReason.broker_transient, False),
    # Missing/partial credentials look identical to an IMDS or role-provider hiccup: retry, never quarantine.
    (NoCredentialsError(), DeliveryReason.broker_transient, False),
    (PartialCredentialsError(provider='env', cred_var=SENTINEL), DeliveryReason.broker_transient, False),
    (NoRegionError(), DeliveryReason.broker_denied, True),
    (ParamValidationError(report=SENTINEL), DeliveryReason.broker_denied, True),
])
def test_client_side_failures_are_classified(envelope, error, reason, permanent):
    publisher = make_publisher(RaisingClient(error), 'https://sqs.us-east-1.amazonaws.com/1/q')
    with pytest.raises(PublishError) as caught:
        publish(publisher, envelope.canonical_bytes())
    assert_safe(caught.value, reason, permanent=permanent)


def test_malformed_acknowledgement_is_transient(envelope):
    class NoMessageId:
        def send_message(self, **kwargs):
            return {'MD5OfMessageBody': 'x'}
    with pytest.raises(PublishError) as caught:
        publish(make_publisher(NoMessageId(), 'https://sqs.us-east-1.amazonaws.com/1/q'), envelope.canonical_bytes())
    assert_safe(caught.value, DeliveryReason.broker_transient, permanent=False)


def test_hung_send_is_bounded_by_the_deadline(envelope):
    release = threading.Event()

    class Hanging:
        def send_message(self, **kwargs):
            release.wait(5)
            return {'MessageId': 'late'}
    publisher = make_publisher(Hanging(), 'https://sqs.us-east-1.amazonaws.com/1/q')
    started = time.monotonic()
    try:
        with pytest.raises(PublishError) as caught:
            publish(publisher, envelope.canonical_bytes(), timeout=0.2)
        assert time.monotonic() - started < 2
        assert_safe(caught.value, DeliveryReason.deadline_expired, permanent=False)
    finally:
        release.set()


def test_closed_publisher_refuses_new_sends(envelope):
    client = CountingClient()
    publisher = make_publisher(client, 'https://sqs.us-east-1.amazonaws.com/1/q', owns_client=True)

    async def scenario():
        assert await publisher.close(time.monotonic() + 1) == 0
        with pytest.raises(PublishError) as caught:
            await publisher.publish(envelope.canonical_bytes(), deadline=time.monotonic() + 1)
        return caught.value
    assert_safe(asyncio.run(scenario()), DeliveryReason.runtime_stopped, permanent=False)
    assert client.closed and client.sends == 0


def test_publisher_never_logs_bodies_or_errors(aws_env, envelope, caplog):
    caplog.set_level(logging.DEBUG, logger='services')
    for code in ('AccessDenied', 'ThrottlingException'):
        with pytest.raises(PublishError):
            publish(make_publisher(stubbed(code), 'https://sqs.us-east-1.amazonaws.com/1/q'),
                    envelope.canonical_bytes())
    ours = [r for r in caplog.records if r.name.startswith('services')]
    assert all(SENTINEL not in r.getMessage() and str(envelope.run_id) not in r.getMessage() for r in ours)


# --- configuration -------------------------------------------------------------------

def enabled_config(**overrides):
    values = dict(enabled=True, database_url='postgresql+asyncpg://u:p@127.0.0.1:1/none',
                  queue_url='http://127.0.0.1:5000/123456789012/eval-runs', identity_key='synthetic-identity')
    values.update(overrides)
    return DeliveryConfig(**values)


def test_from_config_uses_queue_url_timeouts_and_cap(aws_env):
    config = enabled_config(queue_body_bytes=4096, sdk_connect_timeout_seconds=0.5, sdk_read_timeout_seconds=1.5,
                            sdk_attempts=3, send_workers=4)
    publisher = RunEvidencePublisher.from_config(config)
    try:
        client_config = publisher._client.meta.config
        assert publisher.queue_url == config.queue_url and publisher.max_body_bytes == 4096
        assert publisher._client.meta.endpoint_url == 'http://127.0.0.1:5000'
        assert (client_config.connect_timeout, client_config.read_timeout) == (0.5, 1.5)
        assert client_config.retries['total_max_attempts'] == 3
        assert client_config.max_pool_connections == 4 and publisher._io.workers == 4
    finally:
        asyncio.run(publisher.close(time.monotonic() + 1))


@pytest.mark.parametrize('queue_url', [None, '', 'not-a-url', 'ftp://host/1/q'])
def test_from_config_requires_an_http_queue_url(aws_env, queue_url):
    config = DeliveryConfig(queue_url=queue_url)
    with pytest.raises(ValueError):
        RunEvidencePublisher.from_config(config)


def test_default_cap_is_128_kib():
    assert DeliveryConfig().queue_body_bytes == 128 * 1024


def test_publisher_requires_an_explicit_body_cap():
    with pytest.raises(TypeError):
        RunEvidencePublisher(CountingClient(), queue_url='https://sqs.us-east-1.amazonaws.com/1/q', io=BoundedIO(1))
