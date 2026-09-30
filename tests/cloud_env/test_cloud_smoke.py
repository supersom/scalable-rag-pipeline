import importlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest



def _smoke_module():
    return importlib.reload(pytest.importorskip("scripts.smoke_cloud_env"))


class _PortForward:
    def __init__(self, *_args, **_kwargs):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_cloud_smoke_checks_staging_api_and_closes_port_forward(monkeypatch, capsys):
    monkeypatch.setenv("CLOUD_SMOKE_NAMESPACE", "staging-evidence")
    monkeypatch.setenv("CLOUD_SMOKE_PORT", "18080")
    smoke_cloud_env = _smoke_module()
    port_forward_calls = []
    port_forward = _PortForward()
    jwt_secret = "staging-smoke-secret"
    run_id = "2ee9094e-8f6e-4bce-a52c-5d70d1e9a227"
    token_headers = {}
    session_ids = []

    def fake_popen(command, **kwargs):
        port_forward_calls.append((command, kwargs))
        return port_forward

    def fake_run(command, **kwargs):
        if command[1] == "exec" and command[4] == "deployment/api-staging":
            assert "os.environ['JWT_SECRET_KEY']" in command[-1]
            token = smoke_cloud_env.jwt.encode(
                {"sub": "cloud-smoke-user", "role": "user", "exp": 4102444800},
                jwt_secret,
                algorithm="HS256",
            )
            return SimpleNamespace(stdout=json.dumps({
                "token": token, "otel_exporter_endpoint": "http://otel-collector:4317",
                "console_export_enabled": False,
            }))
        if command[1:3] == ["get", "deployments,services,pods"]:
            return SimpleNamespace(stdout="staging-evidence Deployment otel-collector\nstaging-evidence Service otel-collector\n")
        if command[1] == "logs" and any("otel-collector" in arg for arg in command):
            return SimpleNamespace(stdout=(
                "Span #1\n    Trace ID       : 0x" + "1" * 32 +
                "\n    Name           : chat.run\n"
                "Span #2\n    Trace ID       : 0x" + "1" * 32 +
                "\n    Name           : model.call\n"
            ))
        if command[1] == "exec" and command[4] == "deployment/evidence-drain-consumer":
            evidence = {
                "run_id": run_id,
                "completeness": "metadata_only",
                "run_status": "success",
                "trace_id": "1" * 32,
                "model_calls": [{"purpose": "generation", "model": "fake-local-llm"}],
                "operation_events": [{"name": "agent.run", "phase": "complete", "status": "success"}],
                "payload_manifest": [],
                "archive": {"key": f"queue-archive/{run_id}.json", "version_id": "version-1",
                            "size_bytes": 512},
            }
            return SimpleNamespace(stdout=json.dumps(evidence))
        raise AssertionError(f"unexpected kubectl command: {command}")

    class Response:
        def __init__(self, *, headers=None, text=""):
            self.headers = headers or {}
            self.text = text

        def raise_for_status(self):
            return None

    def fake_get(url, timeout):
        assert url in {
            "http://127.0.0.1:18080/health/liveness",
            "http://127.0.0.1:18080/health/readiness",
        }
        assert timeout == 3
        return Response()

    def fake_post(url, *, headers, json, timeout):
        assert url == "http://127.0.0.1:18080/api/v1/chat/stream"
        assert timeout == 30
        token_headers.update(headers)
        session_ids.append(json["session_id"])
        events = [
            {"type": "answer", "content": "This is a deterministic local response.",
             "run_id": run_id, "session_id": json["session_id"]}
        ]
        return Response(headers={"X-Run-ID": run_id}, text="\n".join(map(json_module_dumps, events)))

    json_module_dumps = json.dumps
    monkeypatch.setattr(smoke_cloud_env.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(smoke_cloud_env.subprocess, "run", fake_run)
    monkeypatch.setattr(smoke_cloud_env.httpx, "get", fake_get)
    monkeypatch.setattr(smoke_cloud_env.httpx, "post", fake_post)

    smoke_cloud_env.main()

    assert port_forward_calls[0][0] == [
        "kubectl", "port-forward", "--namespace", "staging-evidence",
        "deployment/api-staging", "18080:8000",
    ]
    assert port_forward.terminated
    assert len(session_ids) == 1 and session_ids[0].startswith("cloud-smoke-")
    claims = smoke_cloud_env.jwt.decode(token_headers["Authorization"].removeprefix("Bearer "),
                                        jwt_secret, algorithms=["HS256"])
    assert claims["sub"] == "cloud-smoke-user"
    assert claims["role"] == "user"
    assert str(UUID(run_id)) == run_id
    report = json.loads(capsys.readouterr().out)
    assert report["api"] == report["fake_models"] == report["stream"] == "ok"
    assert report["otel_collector"]["status"] == "configured"
    assert report["otel_collector"]["api_export_endpoint"] == "http://otel-collector:4317"
    assert report["otel_collector"]["collector_resources"] == [
        {"namespace": "staging-evidence", "kind": "Deployment", "name": "otel-collector"},
        {"namespace": "staging-evidence", "kind": "Service", "name": "otel-collector"},
    ]
    assert report["otel_collector"]["trace"] == {
        "trace_id": "1" * 32, "status": "received", "span_names": ["chat.run", "model.call"]
    }
    assert report["evidence"]["run_id"] == run_id
    assert report["evidence"]["completeness"] == "metadata_only"
    assert report["evidence"]["model_calls"] == [
        {"purpose": "generation", "model": "fake-local-llm"}
    ]
    assert report["evidence"]["archive"]["key"] == f"queue-archive/{run_id}.json"


def test_cloud_smoke_rejects_unexpected_model_answer(monkeypatch):
    monkeypatch.setenv("CLOUD_SMOKE_NAMESPACE", "staging-evidence")
    monkeypatch.setenv("CLOUD_SMOKE_PORT", "18080")
    smoke_cloud_env = _smoke_module()
    port_forward = _PortForward()
    monkeypatch.setattr(smoke_cloud_env.subprocess, "Popen", lambda *_args, **_kwargs: port_forward)
    def fake_run(command, **_kwargs):
        assert command[1] == "exec"
        token = smoke_cloud_env.jwt.encode(
            {"sub": "cloud-smoke-user", "role": "user", "exp": 4102444800},
            "staging-smoke-secret",
            algorithm="HS256",
        )
        assert command[2:4] == ["--namespace", "staging-evidence"]
        return SimpleNamespace(stdout=json_module_dumps({
            "token": token, "otel_exporter_endpoint": "http://otel-collector:4317",
            "console_export_enabled": False,
        }))

    monkeypatch.setattr(
        smoke_cloud_env.subprocess,
        "run",
        fake_run,
    )
    monkeypatch.setattr(smoke_cloud_env.httpx, "get", lambda *_args, **_kwargs: SimpleNamespace(
        raise_for_status=lambda: None
    ))

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(_url, *, json, **_kwargs):
        run_id = "2ee9094e-8f6e-4bce-a52c-5d70d1e9a227"
        response = Response()
        response.headers = {"X-Run-ID": run_id}
        response.text = json_module_dumps({
            "type": "answer", "content": "unexpected answer",
            "run_id": run_id, "session_id": json["session_id"],
        })
        return response

    json_module_dumps = json.dumps
    monkeypatch.setattr(smoke_cloud_env.httpx, "post", fake_post)

    with pytest.raises(RuntimeError, match="deterministic answer"):
        smoke_cloud_env.main()
    assert port_forward.terminated


def test_make_cloud_smoke_invokes_cloud_smoke_script():
    import subprocess

    result = subprocess.run(
        ["make", "--dry-run", "cloud-smoke"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "scripts/smoke_cloud_env.py" in result.stdout
