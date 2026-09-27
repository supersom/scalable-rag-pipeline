import pytest
from pydantic import ValidationError


def test_disabled_defaults_do_not_require_destinations():
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    cfg = DeliveryConfig()
    assert cfg.enabled is False and cfg.capture_content is False
    assert cfg.payload_bytes == 65536 and cfg.pending_runs == 32


def test_metadata_requires_database_queue_and_dedicated_key():
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    for override in ({}, {"database_url": "postgresql://local/db"},
                     {"database_url": "postgresql://local/db", "queue_url": "http://localhost/q"}):
        with pytest.raises(ValidationError):
            DeliveryConfig(enabled=True, **override)
    cfg = DeliveryConfig(enabled=True, database_url="postgresql://local/db",
                         queue_url="http://localhost/q", identity_key="dedicated-key",
                         jwt_secret="jwt-secret")
    assert cfg.enabled and not cfg.capture_content
    with pytest.raises(ValidationError):
        DeliveryConfig(enabled=True, database_url="postgresql://local/db",
                       queue_url="http://localhost/q", identity_key="same", jwt_secret="same")


def test_content_requires_local_synthetic_destinations():
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    base = dict(enabled=True, capture_content=True, database_url="postgresql://local/db",
                queue_url="http://localhost/q", identity_key="dedicated-key", jwt_secret="jwt-secret")
    with pytest.raises(ValidationError):
        DeliveryConfig(**base)
    with pytest.raises(ValidationError):
        DeliveryConfig(**base, environment="prod", content_policy="restricted-synthetic-v1",
                       fixture_manifest="fixtures.json", bucket="evidence", kms_key_id="local-kms",
                       s3_endpoint_url="http://localhost:5000")
    cfg = DeliveryConfig(**base, environment="local", content_policy="restricted-synthetic-v1",
                         fixture_manifest="fixtures.json", bucket="evidence", kms_key_id="local-kms",
                         s3_endpoint_url="http://localhost:5000")
    assert cfg.capture_content


def test_deadline_and_lease_windows_are_consistent():
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    for overrides in ({"commit_reserve_seconds": 20}, {"intent_lease_seconds": 19},
                      {"claim_lease_seconds": 5}, {"payload_bytes": 0},
                      {"content_policy": "unknown"}):
        with pytest.raises(ValidationError):
            DeliveryConfig(**overrides)


def test_from_settings_preserves_disabled_startup(monkeypatch):
    for name, value in {"DATABASE_URL": "postgresql+asyncpg://local/db", "REDIS_URL": "redis://localhost",
                        "NEO4J_PASSWORD": "synthetic", "S3_BUCKET_NAME": "documents",
                        "JWT_SECRET_KEY": "jwt-secret"}.items():
        monkeypatch.setenv(name, value)
    from services.api.app.config import Settings
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    assert DeliveryConfig.from_settings(Settings()).enabled is False
