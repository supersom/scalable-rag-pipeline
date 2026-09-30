#!/usr/bin/env python3
"""Run the deterministic chat smoke contract against the staging EKS API."""

import json
import os
import subprocess
import time
from uuid import UUID, uuid4

import httpx
from jose import jwt


NAMESPACE = os.getenv("CLOUD_SMOKE_NAMESPACE", "staging-evidence")
DEPLOYMENT = os.getenv("CLOUD_SMOKE_API_DEPLOYMENT", "api-staging")
LOCAL_PORT = int(os.getenv("CLOUD_SMOKE_PORT", "18080"))
STARTUP_TIMEOUT = float(os.getenv("CLOUD_SMOKE_STARTUP_TIMEOUT", "45"))
EVIDENCE_TIMEOUT = float(os.getenv("CLOUD_SMOKE_EVIDENCE_TIMEOUT", "75"))
API_BASE_URL = f"http://127.0.0.1:{LOCAL_PORT}"
EXPECTED_ANSWER = "This is a deterministic local response."


def _read_api_runtime() -> dict:
    runtime_script = (
        "import json,os,time; from jose import jwt; "
        "print(json.dumps({'token':jwt.encode({'sub':'cloud-smoke-user','role':'user',"
        "'exp':int(time.time())+300}, os.environ['JWT_SECRET_KEY'], "
        "algorithm=os.getenv('JWT_ALGORITHM','HS256')), "
        "'otel_exporter_endpoint':os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT'), "
        "'console_export_enabled':os.getenv('OTEL_CONSOLE_EXPORT_ENABLED','false').lower()=='true'}))"
    )
    result = subprocess.run(
        [
            "kubectl", "exec", "--namespace", NAMESPACE, f"deployment/{DEPLOYMENT}",
            "--", "python", "-c", runtime_script,
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=15,
    )
    try:
        runtime = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not read runtime configuration from {DEPLOYMENT}") from exc
    if not runtime.get("token"):
        raise RuntimeError(f"could not mint a smoke token inside {DEPLOYMENT} in {NAMESPACE}")
    return runtime


def _collector_trace_report(trace_id: str | None) -> dict:
    if not trace_id:
        return {"trace_id": None, "status": "unavailable", "span_names": []}
    result = subprocess.run(
        [
            "kubectl", "logs", "--namespace", NAMESPACE,
            "deployment/otel-collector", "--since=10m",
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=20,
    )

    # The collector's detailed debug exporter writes spans as multi-line blocks. Extract
    # only span names for this trace: debug output can contain attributes we should not
    # copy into a routine smoke report.
    matching_names = []
    current_trace = None
    current_name = None

    def save_span() -> None:
        if current_trace == trace_id.lower() and current_name:
            matching_names.append(current_name)

    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Span #"):
            save_span()
            current_trace = None
            current_name = None
        elif stripped.startswith("Trace ID") and ":" in stripped:
            current_trace = stripped.split(":", 1)[1].strip().lower().removeprefix("0x")
        elif stripped.startswith("Name") and ":" in stripped:
            current_name = stripped.split(":", 1)[1].strip()
    save_span()
    return {
        "trace_id": trace_id,
        "status": "received" if matching_names else "not_received",
        "span_names": matching_names,
    }


def _otel_report(api_runtime: dict, trace_id: str | None) -> dict:
    result = subprocess.run(
        [
            "kubectl", "get", "deployments,services,pods", "--all-namespaces",
            "-o", "custom-columns=NAMESPACE:.metadata.namespace,KIND:.kind,NAME:.metadata.name",
            "--no-headers",
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=20,
    )
    collector_resources = []
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) >= 3 and any(token in " ".join(columns).lower() for token in ("otel", "collector")):
            collector_resources.append({"namespace": columns[0], "kind": columns[1], "name": columns[2]})

    endpoint = api_runtime.get("otel_exporter_endpoint")
    if not endpoint:
        status = "not_configured"
        trace_status = "API has no OTLP endpoint; it exports no spans to a collector."
    elif collector_resources:
        status = "configured"
        trace_status = "Collector is present; matching spans are shown below."
    else:
        status = "collector_not_found"
        trace_status = "API has an OTLP endpoint, but no collector resource was found in the cluster."
    return {
        "status": status,
        "api_export_endpoint": endpoint,
        "console_export_enabled": api_runtime.get("console_export_enabled", False),
        "collector_resources": collector_resources,
        "trace_status": trace_status,
        "trace": _collector_trace_report(trace_id) if endpoint and collector_resources else {
            "trace_id": trace_id, "status": "unavailable", "span_names": [],
        },
    }


_EVIDENCE_SUMMARY_SCRIPT = r'''import json, os, sys, time
import boto3
from botocore.exceptions import ClientError

run_id = sys.argv[1]
bucket = os.environ.get("EVIDENCE_ARCHIVE_BUCKET")
if not bucket:
    raise SystemExit("evidence archive bucket is not configured")
client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
deadline = time.monotonic() + float(os.getenv("CLOUD_SMOKE_EVIDENCE_TIMEOUT", "75"))
while True:
    try:
        obj = client.get_object(Bucket=bucket, Key=f"queue-archive/{run_id}.json")
        envelope = json.loads(obj["Body"].read())
        break
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code not in {"NoSuchKey", "NotFound", "404"} or time.monotonic() >= deadline:
            raise SystemExit("evidence archive object is unavailable")
        time.sleep(1)

run = envelope["safe_run"]
calls = run.get("model_calls", [])
retrieval = run.get("retrieval", [])
manifest = envelope.get("manifest", [])
summary = {
    "run_id": str(envelope["run_id"]),
    "completeness": envelope["completeness"],
    "redaction_policy_version": envelope["redaction_policy_version"],
    "run_status": run["status"],
    "trace_id": run.get("trace_id"),
    "operations": run.get("operations", {}),
    "configuration": run.get("configuration", {}),
    "model_calls": [{key: call.get(key) for key in
        ("ordinal", "purpose", "model", "status", "latency_ms", "input_tokens", "output_tokens")}
        for call in calls],
    "retrieval": [{"collection": item.get("collection"), "vector_status": item.get("vector_status"),
        "graph_status": item.get("graph_status"), "item_count": len(item.get("items", []))}
        for item in retrieval],
    "trajectory": [{key: step.get(key) for key in ("step", "tool_name", "status", "latency_ms")}
        for step in run.get("trajectory", [])],
    "dependencies": [{key: item.get(key) for key in ("sequence", "name", "status", "latency_ms")}
        for item in run.get("dependencies", [])],
    "operation_events": [{key: item.get(key) for key in ("name", "phase", "status", "latency_ms")}
        for item in run.get("operation_events", [])],
    "payload_manifest": [{"kind": item["slot"]["kind"], "state": item["state"],
        "reason": item.get("reason")} for item in manifest],
    "archive": {"bucket": bucket, "key": f"queue-archive/{run_id}.json",
        "version_id": obj.get("VersionId"), "size_bytes": obj.get("ContentLength")},
}
print(json.dumps(summary, sort_keys=True))
'''


def _read_archived_evidence(run_id: str) -> dict:
    try:
        result = subprocess.run(
            [
                "kubectl", "exec", "--namespace", NAMESPACE,
                "deployment/evidence-drain-consumer", "--", "python", "-c",
                _EVIDENCE_SUMMARY_SCRIPT, run_id,
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=EVIDENCE_TIMEOUT + 15,
        )
        summary = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"could not inspect archived evidence for run {run_id}; check the evidence worker and drain consumer"
        ) from exc
    if summary.get("run_id") != run_id:
        raise RuntimeError("evidence archive returned a different run ID")
    return summary


def _wait_for_api(port_forward: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        exit_code = port_forward.poll()
        if exit_code is not None:
            raise RuntimeError(f"kubectl port-forward exited early with status {exit_code}")
        try:
            for path in ("/health/liveness", "/health/readiness"):
                response = httpx.get(f"{API_BASE_URL}{path}", timeout=3)
                response.raise_for_status()
            return
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"staging API did not become healthy through port-forward: {last_error}")


def _stop_port_forward(port_forward: subprocess.Popen) -> None:
    if port_forward.poll() is not None:
        return
    port_forward.terminate()
    try:
        port_forward.wait(timeout=5)
    except subprocess.TimeoutExpired:
        port_forward.kill()
        port_forward.wait(timeout=5)


def main() -> None:
    port_forward = subprocess.Popen(
        [
            "kubectl", "port-forward", "--namespace", NAMESPACE,
            f"deployment/{DEPLOYMENT}", f"{LOCAL_PORT}:8000",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_api(port_forward)
        api_runtime = _read_api_runtime()
        token = api_runtime["token"]
        session_id = f"cloud-smoke-{uuid4()}"
        response = httpx.post(
            f"{API_BASE_URL}/api/v1/chat/stream",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": f"hello cloud smoke {time.time_ns()}", "session_id": session_id},
            timeout=30,
        )
        response.raise_for_status()
        try:
            events = [json.loads(line) for line in response.text.splitlines() if line]
        except json.JSONDecodeError as exc:
            raise RuntimeError("staging chat stream returned invalid JSON") from exc

        run_id = response.headers.get("X-Run-ID")
        if not run_id:
            raise RuntimeError("staging chat stream returned no X-Run-ID header")
        try:
            UUID(run_id)
        except ValueError as exc:
            raise RuntimeError("staging chat stream returned an invalid X-Run-ID") from exc
        if not events or any(
            event.get("run_id") != run_id or event.get("session_id") != session_id
            for event in events
        ):
            raise RuntimeError("staging chat events have missing or inconsistent run/session IDs")
        if any(event.get("type") == "error" for event in events):
            raise RuntimeError("staging chat stream returned an error event")
        if not any(
            event.get("type") == "answer" and event.get("content") == EXPECTED_ANSWER
            for event in events
        ):
            raise RuntimeError("staging fake-models path returned no deterministic answer")
        evidence = _read_archived_evidence(run_id)
        otel = _otel_report(api_runtime, evidence.get("trace_id"))
    finally:
        _stop_port_forward(port_forward)

    print(json.dumps({
        "api": "ok", "fake_models": "ok", "stream": "ok",
        "otel_collector": otel, "evidence": evidence,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
