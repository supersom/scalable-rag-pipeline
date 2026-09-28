import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from services.aws_emulator import healthcheck, init_s3


def _s3_client():
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_initialize_creates_and_versions_missing_bucket():
    client = _s3_client()
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": []})
        stubber.add_response(
            "create_bucket",
            {},
            {"Bucket": "eval-traces"},
        )
        stubber.add_response(
            "put_bucket_versioning",
            {},
            {
                "Bucket": "eval-traces",
                "VersioningConfiguration": {"Status": "Enabled"},
            },
        )

        init_s3.initialize(client=client, sleep=lambda _: None)


def test_initialize_accepts_bucket_already_owned_by_caller():
    client = _s3_client()
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": []})
        stubber.add_client_error(
            "create_bucket",
            service_error_code="BucketAlreadyOwnedByYou",
            service_message="owned",
            expected_params={"Bucket": "eval-traces"},
        )
        stubber.add_response(
            "put_bucket_versioning",
            {},
            {
                "Bucket": "eval-traces",
                "VersioningConfiguration": {"Status": "Enabled"},
            },
        )

        init_s3.initialize(client=client, sleep=lambda _: None)


def test_initialize_retries_endpoint_connection_failures():
    class EventuallyAvailableS3:
        def __init__(self):
            self.list_attempts = 0
            self.versioning = None

        def list_buckets(self):
            self.list_attempts += 1
            if self.list_attempts < 3:
                raise EndpointConnectionError(endpoint_url="http://s3-emulator:4566")
            return {"Buckets": []}

        def create_bucket(self, *, Bucket):
            assert Bucket == "eval-traces"

        def put_bucket_versioning(self, *, Bucket, VersioningConfiguration):
            self.versioning = (Bucket, VersioningConfiguration)

    client = EventuallyAvailableS3()
    sleeps = []

    init_s3.initialize(client=client, sleep=sleeps.append)

    assert client.list_attempts == 3
    assert sleeps == [0.25, 0.25]
    assert client.versioning == ("eval-traces", {"Status": "Enabled"})


def test_initialize_stops_after_sixty_connection_failures():
    class UnavailableS3:
        def __init__(self):
            self.list_attempts = 0

        def list_buckets(self):
            self.list_attempts += 1
            raise EndpointConnectionError(endpoint_url="http://s3-emulator:4566")

    client = UnavailableS3()
    sleeps = []

    with pytest.raises(EndpointConnectionError):
        init_s3.initialize(client=client, sleep=sleeps.append)

    assert client.list_attempts == 60
    assert sleeps == [0.25] * 59


def test_initialize_propagates_unexpected_create_error():
    client = _s3_client()
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": []})
        stubber.add_client_error(
            "create_bucket",
            service_error_code="AccessDenied",
            service_message="denied",
            http_status_code=403,
            expected_params={"Bucket": "eval-traces"},
        )

        with pytest.raises(ClientError, match="AccessDenied"):
            init_s3.initialize(client=client, sleep=lambda _: None)


def test_healthcheck_accepts_versioned_bucket():
    client = _s3_client()
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_bucket_versioning",
            {"Status": "Enabled"},
            {"Bucket": "eval-traces"},
        )

        healthcheck.main(client=client)


@pytest.mark.parametrize("response", [{}, {"Status": "Suspended"}])
def test_healthcheck_rejects_bucket_without_enabled_versioning(response):
    client = _s3_client()
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_bucket_versioning",
            response,
            {"Bucket": "eval-traces"},
        )

        with pytest.raises(SystemExit, match="is not ready"):
            healthcheck.main(client=client)


class _EventuallyAvailableSQS:
    def __init__(self, failures):
        self.failures = failures
        self.list_attempts = 0

    def list_queues(self):
        self.list_attempts += 1
        if self.list_attempts <= self.failures:
            raise EndpointConnectionError(endpoint_url="http://s3-emulator:4566")
        return {}


def test_queue_initialize_waits_for_emulator_then_ensures_queues(monkeypatch):
    from services.aws_emulator import init_eval_queue

    ensured = []
    monkeypatch.setattr(init_eval_queue, "ensure_queues",
                        lambda client, queue, dlq: ensured.append((queue, dlq)) or {})
    client = _EventuallyAvailableSQS(failures=2)
    sleeps = []

    init_eval_queue.initialize(client=client, sleep=sleeps.append)

    assert client.list_attempts == 3
    assert sleeps == [0.25, 0.25]
    assert ensured == [("eval-runs", "eval-runs-dlq")]


def test_queue_initialize_stops_after_sixty_connection_failures():
    from services.aws_emulator import init_eval_queue

    client = _EventuallyAvailableSQS(failures=10**6)
    sleeps = []

    with pytest.raises(EndpointConnectionError):
        init_eval_queue.initialize(client=client, sleep=sleeps.append)

    assert client.list_attempts == 60
    assert sleeps == [0.25] * 59


def test_queue_client_uses_the_shared_emulator_endpoint():
    from services.aws_emulator import init_eval_queue

    client = init_eval_queue._client()

    assert client.meta.service_model.service_name == "sqs"
    assert client.meta.endpoint_url == init_s3.ENDPOINT_URL
    assert client.meta.region_name == init_s3.REGION


def test_s3_healthcheck_is_still_the_bucket_versioning_check():
    import inspect

    source = inspect.getsource(healthcheck)
    assert "get_bucket_versioning" in source
    assert "sqs" not in source.lower()
