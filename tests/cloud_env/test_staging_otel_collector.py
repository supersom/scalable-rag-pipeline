def test_staging_deployer_applies_local_style_collector_before_api():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    deployer = (root / "scripts/deploy_staging_evidence.sh").read_text()
    manifest_path = root / "deploy/k8s/staging-otel-collector.yaml"
    manifest = manifest_path.read_text()
    assert 'kubectl apply -f "${ROOT_DIR}/deploy/k8s/staging-otel-collector.yaml"' in deployer
    assert deployer.index("staging-otel-collector.yaml") < deployer.index('helm upgrade --install api-staging')
    assert 'OTEL_EXPORTER_OTLP_ENDPOINT\n    value: "http://otel-collector:4317"' in deployer
    assert "opentelemetry-collector-contrib:0.92.0" in manifest
    assert "4317" in manifest and "4318" in manifest
    assert "verbosity: detailed" in manifest
