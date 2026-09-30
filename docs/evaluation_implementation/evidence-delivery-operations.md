# Evidence delivery: local operation and limits

Operating guide for the PR 4 evidence path: redacted synthetic payloads in versioned object storage, a transactional PostgreSQL outbox, and a standalone queue publisher. It describes what runs locally today. It is not a production runbook: production enablement is the separate PR 5 gate described in [Production gates](#production-gates-that-local-success-does-not-satisfy).

**Related documents:** [PR 4 design](pr-04-payload-publication-design.md), [Gap 1 progress](gap-01-production-run-instrumentation.md), [local environment](local-environment.md).

## What the local path guarantees and does not guarantee

- **Committed evidence is delivered at least once.** After the outbox transaction commits, a worker restart or broker outage does not lose the record; it is re-sent with identical bytes until acknowledged, quarantined, or expired. SQS standard delivery is at-least-once, so consumers must deduplicate on `(export_version, run_schema_version, run_id)` and compare `export_hash`.
- **Admission is not durability.** The API returns `admitted` as soon as terminal evidence enters a bounded in-memory queue. Only `committed` means the run row, outbox row, and payload references exist in PostgreSQL.
- **Evidence never changes the answer.** Admission, upload, and commit failures are counted and dropped; the streamed NDJSON response is unchanged.
- **This is not audit-grade capture.** Runs can be lost before commit, and no claim is made that every request is recorded.
- **This is not general data-loss prevention.** Redaction is a finite, versioned pattern set. Only `input` and `retrieval_chunk` payloads must match a fixture hash in the manifest; model requests, model responses, outputs, and retrieval queries are approved through derived provenance from those sources, so their text is protected only by the patterns. Measured on 2026-09-28, these pass through unredacted: JSON-quoted `"password": "..."`, `--password x`, `passwd=`/`pwd=`, `Authorization: Basic ...`, `Cookie:` values, credentials embedded in URLs, and `sk-`/`ghp_`-style API keys; a `token:abc def` value is redacted only up to the first space. Use content capture only with the fake models and synthetic fixtures. Do not point it at a real model, such as local Ollama, or real data.

## Disabled by default, opt-in for local synthetic capture

| Mode | How it starts | Behavior |
|---|---|---|
| Default | `make local-up`, or any deployment without the evidence flags | `EVAL_EVIDENCE_ENABLED=false` and `EVAL_CAPTURE_CONTENT=false`. No runtime, buffers, S3/SQS clients, or background work are created. PR 3 behavior is unchanged. |
| Opt-in evidence | `make local-evidence-up` | Layers `docker-compose.evaluation.yml` over the base file. Runs a one-shot `evidence-migrate` job, recreates `api-local` with delivery and synthetic content capture enabled, and starts the `evidence-delivery` worker. |

Never enable the evidence flags in `.env`, `.env.example`, or `.env.evidence`; the selected base file sets both to `false` and carries the queue, bucket, and KMS values the override reuses. The Git-ignored `.env.evidence` is used when present; otherwise `make` uses the safe `.env.example` and forces fake-model settings even if hosted credentials are exported in the shell. The override is the only place evidence flags are enabled, and it supplies its own dummy identity key, literal dummy AWS credentials (so exported developer credentials are not passed through), and the synthetic manifest mount. Content capture is only safe with synthetic inputs and fake models; do not use hosted providers with this opt-in profile.

The override reuses the `scalable-rag-eval` project, so `make local-evidence-up` **recreates `api-local` in the running local stack** and adds the migration job and worker. `make local-evidence-down` stops the stack without removing volumes. Do not run either against a stack whose services you do not want recreated; use a separate Compose project name if you need both modes at once.

Content capture is rejected outside `ENV=local|test`, without the `restricted-synthetic-v1` policy, or without a fixture manifest, bucket, KMS key ID, S3 endpoint, and server-selected fixture case. Metadata-only delivery (`EVAL_EVIDENCE_ENABLED=true`, `EVAL_CAPTURE_CONTENT=false`) needs only the database, queue URL, and identity key.

## Operator commands

```bash
make local-evidence-check    # compose config + evidence contract tests; no running stack needed
make local-evidence-up       # build, migrate, start API + worker; waits up to EVIDENCE_WAIT_SECONDS (default 300)
make local-evidence-smoke    # needs the stack from local-evidence-up
make local-evidence-down     # stops; never passes -v, so volumes survive
```

`make local-evidence-smoke` runs `tests/local_env/test_eval_queue.py` and `test_running_stack.py` with `RUN_LOCAL_EVIDENCE_TESTS=1`. It checks that `evidence-migrate` exited 0, `evidence-delivery` is running, `api-local` and `s3-emulator` are healthy, and the queue, DLQ, and redrive policy are ready, then repeats the default smoke contract. It does not send a run through the pipeline; end-to-end linkage of run rows, queue envelopes, and object versions is proven by the [live acceptance suite](#verification-commands).

Local endpoints (all loopback): API `http://127.0.0.1:18000`, PostgreSQL `127.0.0.1:15432`, Moto S3 and SQS `http://127.0.0.1:14566`, fake models `http://127.0.0.1:8001`. The queue URL is `http://localhost:14566/123456789012/eval-runs` from the host and `http://s3-emulator:4566/123456789012/eval-runs` inside Compose; `123456789012` is Moto's default account.

Maintenance commands run in a one-off container from the same image and environment as the worker:

```bash
ENV_FILE=.env.example; test ! -f .env.evidence || ENV_FILE=.env.evidence
EVIDENCE_RUN="docker compose -p scalable-rag-eval --profile eval-local --env-file $ENV_FILE -f docker-compose.yml -f docker-compose.evaluation.yml run --rm evidence-delivery python -m services.evaluation_delivery.maintenance"

$EVIDENCE_RUN cleanup --dry-run --limit 100
$EVIDENCE_RUN cleanup --apply --limit 100
$EVIDENCE_RUN cleanup --apply --limit 100 --namespace <exact-run-namespace>
$EVIDENCE_RUN retry --run-id <uuid> --export-version 1.0 --run-schema-version 1.0
```

Nothing schedules these locally; an operator runs them.

These commands were run against a disposable copy of the stack (a different project name and host ports, cached images, and the working tree mounted read-only) rather than the shared `scalable-rag-eval` project. The dry-run and apply commands each printed the fixed-key line with all counts zero and exited 0. A retry for an unknown run printed `retry status=not_requeued` and exited 3. `cleanup` without `--dry-run` or `--apply`, and `--limit 0`, printed `evidence maintenance arguments invalid` and exited 2.

## Signed synthetic tenants

Capture requires a tenant, taken only from the optional `tenant_id` claim of an already verified JWT. A body field, header, retrieved document, or model output can never select a tenant. A missing or malformed claim produces a run with no content and no tenant-scoped hashes; no user pseudonym is exported in any case; chat authorization is unaffected.

The checked-in manifest `deploy/evaluation/restricted-synthetic-v1.json` approves two synthetic tenants, `synthetic-tenant` and `synthetic-tenant-2`, with the same seven input cases plus history, template, and retrieval-chunk sources. Approval is by explicit tenant, case, source kind, source ID, and content hash, never by a `safe` flag in a document. Only case `literal` also approves an application responder template (`responder-direct-v1`). Adding a case means editing the manifest and its hashes together with the policy tests.

The fixture case is server configuration (`EVAL_FIXTURE_CASE`, `synthetic-question` in the Compose override), not something a request selects. Because the manifest approves no responder template for `synthetic-question`, model request, response, and output content for that case would be denied as `unapproved_source` and the run would be `partial`. In a disposable copy of the Compose stack, a request signed for `synthetic-tenant` with arbitrary unapproved text produced a `partial` run with no payload references, and the outbox row reached `delivered` after one attempt; requests without a tenant produced `metadata_only`. The live acceptance suite uses `literal`, which is the only case that exercises complete linked content.

A local token is signed with `JWT_SECRET_KEY` (`local-dev-only-change-me` in `.env.example`) and `HS256`:

```python
import time
from jose import jwt  # python-jose, the API's pinned JWT library
token = jwt.encode({"sub": "synthetic-user", "tenant_id": "synthetic-tenant", "exp": time.time() + 300},
                   "local-dev-only-change-me", algorithm="HS256")
```

The identity key (`EVAL_IDENTITY_HASH_KEY`) that keys tenant, session, digest, and source pseudonyms must differ from the JWT secret; startup rejects reuse.

## Configuration and limits

All variables are read from the environment by `Settings`. Configuration is validated before any client is created. The worker and maintenance command exit 2 on invalid values with a fixed message that never echoes URLs or keys.

| Variable | Default | Meaning |
|---|---|---|
| `EVAL_EVIDENCE_ENABLED`, `EVAL_CAPTURE_CONTENT` | `false`, `false` | Delivery switch; content capture requires delivery |
| `EVAL_QUEUE_URL`, `EVAL_IDENTITY_HASH_KEY` | unset | Required when enabled, together with `DATABASE_URL` |
| `EVAL_CONTENT_POLICY`, `EVAL_FIXTURE_MANIFEST`, `EVAL_FIXTURE_CASE` | unset | Required for content capture; policy must be `restricted-synthetic-v1` |
| `EVAL_S3_BUCKET`, `EVAL_KMS_KEY_ID`, `EVAL_S3_ENDPOINT_URL` | unset | Required for content capture; every write sends `aws:kms` with this key ID |
| `EVAL_PAYLOAD_BYTES` / `EVAL_RUN_BYTES` / `EVAL_PAYLOAD_SLOTS` | 64 KiB / 1 MiB / 64 | Per-payload, per-run, and slot caps |
| `EVAL_ACTIVE_RUNS` / `EVAL_ACTIVE_BYTES` | 32 / 16 MiB | Capture buffers held by running requests |
| `EVAL_PENDING_RUNS` / `EVAL_PENDING_BYTES` | 32 / 16 MiB | Terminal admissions awaiting upload and commit; overflow drops the run |
| `EVAL_RUNTIME_WORKERS` / `EVAL_S3_WORKERS` | 2 / 2 | Upload/commit tasks and S3 executor threads |
| `EVAL_DEADLINE_SECONDS` / `EVAL_COMMIT_RESERVE_SECONDS` | 20 / 3 | Total upload-and-commit budget; last seconds reserved for the metadata commit |
| `EVAL_SDK_CONNECT_TIMEOUT_SECONDS` / `EVAL_SDK_READ_TIMEOUT_SECONDS` / `EVAL_SDK_ATTEMPTS` | 1 / 2 / 2 | Finite SDK waits |
| `EVAL_QUEUE_BODY_BYTES` | 128 KiB | Larger envelopes are rejected, never truncated |
| `EVAL_DELIVERY_BATCH` / `EVAL_SEND_WORKERS` | 10 / 2 | Worker claim size and send concurrency |
| `EVAL_CLAIM_LEASE_SECONDS` / `EVAL_ACK_RESERVE_SECONDS` | 60 / 5 | Outbox claim lease and acknowledgement reserve |
| `EVAL_PRODUCER_ATTEMPTS` / `EVAL_BACKOFF_BASE_SECONDS` / `EVAL_BACKOFF_CAP_SECONDS` | 8 / 1 / 60 | Retry budget and jittered exponential backoff |
| `EVAL_SHUTDOWN_DRAIN_SECONDS` | 10 | Bounded drain on shutdown |
| `EVAL_INTENT_LEASE_SECONDS` / `EVAL_ORPHAN_GRACE_SECONDS` | 60 / 86400 | Upload-intent lease; age before an unreferenced version is an orphan |
| `EVAL_CONTENT_RETENTION_DAYS` / `EVAL_METADATA_RETENTION_DAYS` | 30 / 90 | Content expires from object creation; metadata from run completion |

Cross-field rules: the commit reserve must be shorter than the deadline; the intent lease must cover the deadline; the claim lease must cover the SDK's full send budget plus the acknowledgement reserve; the backoff cap must be at least the base; and `payload <= run <= active` and `run <= pending` byte budgets must hold. Nothing enforces `EVAL_CONTENT_RETENTION_DAYS <= EVAL_METADATA_RETENTION_DAYS`. Reads also require each reference's stored expiry to equal creation plus the current content retention setting, so changing `EVAL_CONTENT_RETENTION_DAYS` makes every existing reference unreadable; treat it as a data migration, not a tuning knob. Configuration is also validated when evidence is disabled: a malformed unused `EVAL_*` value (for example `EVAL_DEADLINE_SECONDS=90`, or `EVAL_CAPTURE_CONTENT=true` without delivery) fails API startup. The Helm values and the base Compose file set none, so they are unaffected. These are engineering defaults for local verification, not service-level objectives; production values need load testing.

## Migrations

```bash
python -m services.api.app.evaluation.migrate      # uses DATABASE_URL
```

`make local-evidence-up` runs this as the `evidence-migrate` job, and both the API and worker wait for it to complete. Migrations are numbered SQL files applied in one transaction under a PostgreSQL advisory lock, so concurrent starts serialize and a failure leaves nothing half-applied. Each applied file's SHA-256 is recorded; an edited or missing applied file is a conflict, not silently skipped. There is no drop-and-recreate path. The chat-history `create_all()` bootstrap is unrelated and does not manage these tables.

## Admission, commit, and where evidence can be lost

1. **Capture.** Eligible content is redacted immediately into a bounded per-request buffer. Rejected content becomes a fixed unavailable reason, never a copy.
2. **Admission.** At the terminal state the run is detached once and submitted. The result is `admitted`, `disabled`, `dropped`, or `failed`. `admitted` promises nothing about durability.
3. **Upload and commit.** A worker records an upload intent, uploads each payload, then commits the run, outbox row, and references in one transaction. `committed` is reported only after that transaction succeeds.
4. **Delivery.** The standalone worker claims committed rows, sends the exact stored envelope, and marks the row delivered only after broker acknowledgement.

Loss windows:

- **Before commit.** A process crash, pending-queue overflow (`pending_capacity`), shutdown drain, PostgreSQL outage, or expired deadline can lose the run. Drops are counted, not retried. Payloads already uploaded may remain as orphan versions until cleanup.
- **After commit.** The run survives worker and broker failure. If the broker acknowledged but the database update failed, a later claim re-sends identical bytes; consumers must accept the identical duplicate once.
- **Conflicts.** The same identity with a different `export_hash` is a conflict and never overwrites the stored evidence. A consumer that sees it must quarantine, not merge.

Failed or denied payloads do not discard the run. Safe metadata and payloads that uploaded successfully are preserved, and the envelope's completeness is `complete`, `partial`, or `metadata_only`. `complete` describes the observed eligible payload slots, not answer quality.

### Quarantine and expiry

A record is quarantined for a permanent failure (for example a denied broker) or after `EVAL_PRODUCER_ATTEMPTS` failed attempts. Only the explicit `retry` command returns it to delivery, preserving its identity, stored bytes, and hash. Retry never revives a record past metadata expiry. Expired records are not sent, and metadata expiry deletes the run, outbox, and reference rows even if quarantined; retention takes precedence over retry. This producer quarantine is separate from the SQS consumer DLQ (`eval-runs-dlq`). Exhaustion detected when a lapsed lease is reclaimed quarantines without a log line, and the worker's `retried`/`quarantined` counter is incremented before the database update is known to have applied, so for exhausted retries these counts are approximate; query `evaluation_outbox` for the truth.

## Payload access and authorization

There is no HTTP resolver endpoint. `S3PayloadStore.get(reference, authorized_tenant, deadline=...)` is the only read path, for trusted server-side code. It requires the tenant partition derived from a verified token, and before any object read it checks that partition, the configured bucket and key prefix, the policy version, and the expiry. It then reads the exact stored version and verifies size and SHA-256 before returning bytes. A caller-supplied reference cannot select another bucket, tenant, or key namespace, and a mismatched or missing tenant is denied without touching storage. A reference to expired content is refused even when physical deletion is delayed.

The `PayloadRef` hash covers the redacted stored bytes. Source-content digests in the envelope are separate, domain-separated tenant HMACs of the original digest.

## Retention, cleanup, and retry

| Data | Expires | Removed by |
|---|---|---|
| Content object versions | 30 days after object creation | `cleanup --apply` (all versions and delete markers, not a new delete marker) |
| Run, outbox, and reference rows, including quarantined | 90 days after run completion | `cleanup --apply` |
| Unreferenced versions from failed or abandoned uploads | 24 hours after the upload intent expires | `cleanup --apply`, only with no live upload intent, and only for a namespace with no live reference |

A duplicate admission never extends either expiry. Cleanup is idempotent, paginated, and bounded by `--limit`. Orphan removal first records a cleanup-owned marker in PostgreSQL under a short transaction, so a slow commit cannot attach a reference to an object cleanup is deleting. If PostgreSQL is unavailable, deletion is deferred and reported as a failure with reason `database_unavailable`. Eligibility is per run namespace, not per object version: if one upload timed out but its object was still written, and the run's other payloads committed, the stray version survives until those references expire (up to 30 days) rather than 24 hours. It stays inside the content retention window, so it is a cleanup-latency gap and not an exposure past retention.

Without `--namespace`, cleanup discovers candidates from its coordination table and enumerates only the configured evidence bucket and prefix. `--namespace` accepts one exact run namespace and may be repeated; there is no bucket-wide, table-wide, or database-wide reset. `retry` requires an exact run ID and both version strings; there is no retry-all.

Output is one line of fixed keys and never includes object keys, run IDs, payload bytes, or exception messages:

```text
cleanup mode=dry-run runs_expired=0 namespaces_examined=0 namespaces_eligible=0 namespaces_cleaned=0 versions_examined=0 versions_deleted=0 failures=0
retry status=requeued
```

Exit codes: `0` done, `1` runtime failure, `2` invalid arguments or configuration, `3` incomplete (cleanup failures, or a retry that was not requeued). Always run `--dry-run` first; it reads and reports without mutating.

Queue copies, trace stores, backups, and datasets have their own retention. The 30 and 90 day windows apply only to the object versions and PostgreSQL rows above, and nothing here grants queued or backed-up copies additional time or deletes them.

## Metrics and known observability gaps

`services/api/app/evaluation/delivery_metrics.py` defines the metrics, with labels restricted to fixed component, outcome, and reason enums. No run, tenant, user, key, path, exception message, or queue body can become a label value.

| Metric | Type | Labels |
|---|---|---|
| `evidence_delivery_events_total` | counter | `component`, `outcome`, `reason` |
| `evidence_pending_runs`, `evidence_pending_bytes`, `evidence_pending_oldest_age_seconds` | gauge | none |
| `evidence_quarantine_records`, `evidence_quarantine_oldest_age_seconds` | gauge | none |
| `evidence_processing_seconds` | histogram | `component` |
| `evidence_payload_bytes` | histogram | `component` |

Components: `capture`, `admission`, `runtime`, `upload`, `commit`, `publisher`, `worker`, `cleanup`, `maintenance`. Outcomes: `attempted`, `admitted`, `omitted`, `dropped`, `stored`, `committed`, `failed`, `retried`, `delivered`, `quarantined`, `cleaned`, `disabled`. Reasons are the fixed payload-unavailable and delivery reason enums, or `none`.

Three gaps remain in the local implementation. Treat them as open items, not working monitoring:

1. **Nothing exposes these metrics.** They are recorded in the API process's in-process Prometheus registry, but no `/metrics` route or exporter serves it (the API-wide `/metrics` gap is tracked separately in the backlog). Tests assert the metrics with an injected registry.
2. **Only the API runtime records metrics.** It produces session-level capture omissions, admission, runtime, and upload events, a `commit` latency observation (no `commit` counter), and the pending gauges. Per-slot capture denials (`unapproved_source`, `redaction_failed`, `payload_too_large`, and similar) are recorded in the payload manifest but emit no metric. The worker and maintenance entrypoints do not construct a metrics object, so the `publisher`, `worker`, `cleanup`, and `maintenance` components (including `delivered`, worker-side `retried`/`quarantined`, and `cleaned`) are recorded only when a test injects one.
3. **The quarantine gauges are never set.** Nothing calls the gauge update, so `evidence_quarantine_records` and its age stay at zero. Inspect quarantine directly in PostgreSQL (`evaluation_outbox` rows with `state = 'quarantined'`; the other tables are `evaluation_runs`, `evaluation_payload_refs`, and `evaluation_upload_intents`) until this is wired.

Logs from the worker and maintenance command use fixed messages and reason codes only.

## Verification commands

Live acceptance tests need the local stack's PostgreSQL, Moto, and fake models. They use per-test schemas, unique bucket and queue names, and refuse to reuse an existing bucket or queue name; they never consume a shared queue.

```bash
LIVE="RUN_EVAL_DELIVERY_TESTS=1 EVAL_TEST_DATABASE_URL=postgresql+asyncpg://ragadmin:changeme@127.0.0.1:15432/rag_db \
  EVAL_TEST_API_URL=http://127.0.0.1:18080 EVAL_TEST_S3_ENDPOINT=http://127.0.0.1:14566 \
  EVAL_TEST_SQS_ENDPOINT=http://127.0.0.1:14566 EVAL_TEST_MODEL_URL=http://127.0.0.1:8001"

pytest -q
RUN_EVAL_POSTGRES_TESTS=1 EVAL_TEST_DATABASE_URL=postgresql+asyncpg://ragadmin:changeme@127.0.0.1:15432/rag_db \
  pytest tests/evaluation_delivery/test_outbox_postgres.py tests/evaluation_delivery/test_migrations.py -q
env $LIVE pytest tests/evaluation_delivery/test_delivery_acceptance.py \
  tests/evaluation_delivery/test_restart_acceptance.py tests/evaluation_delivery/test_retention_acceptance.py -q
```

Run the observability and delivery suites in the pinned API image with a read-only checkout mount:

```bash
docker run --rm --entrypoint sh -v "$PWD:/repo:ro" -w /repo <api-local image> -c \
  'pip install -q -r requirements-evaluation-test.txt && python -m pytest tests/observability tests/evaluation_delivery -q -p no:cacheprovider'
```

Recorded counts for PR 4 are in the [Gap 1 verification record](gap-01-production-run-instrumentation.md#pr-4-verification).

## Shared EKS prerequisite for staging

The shared `rag-platform-cluster` has one Terraform owner: `infra/terraform`. The staging root
(`infra/terraform-staging`) is a consumer; it looks up the existing EKS cluster and related VPC,
OIDC, and subnet resources and must not create or adopt a cluster. If a cluster exists in AWS but
is not tracked by the canonical state, stop and deliberately import/reconcile it there before
continuing. Do not let a second Terraform state manage the same EKS cluster.

Review the complete canonical plan before applying it. This root owns resources beyond EKS, so its
plan can include unrelated platform changes; do not hide them with `-target`. Apply only after the
full plan has been reviewed and explicitly approved:

Run this sequence one command at a time; do not paste it as a batch. Stop after each `plan` to
review its complete output, and obtain explicit approval immediately before the corresponding
`apply`.

```bash
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform plan
# Review the complete plan; it may include non-EKS resources owned by this root.
# Only after review/approval:
terraform -chdir=infra/terraform apply

bash scripts/bootstrap_cluster.sh --start 1 --end 1
terraform -chdir=infra/terraform-staging init
terraform -chdir=infra/terraform-staging plan
# Review the staging plan, then apply it explicitly:
terraform -chdir=infra/terraform-staging apply

bash scripts/deploy_staging_evidence.sh
```

Bootstrap stage 1 only updates local kubeconfig. The baseline managed nodes carry the
`CriticalAddonsOnly=true:NoSchedule` taint, so ordinary staging pods cannot run on them. Before
deploying staging workloads, run bootstrap stages 2–3 to install the shared-cluster Karpenter
controller and CPU/GPU NodePools. The CPU NodePool supplies untainted capacity on demand; this can
launch billable Spot or on-demand EC2 instances. KubeRay stage 7 remains unnecessary for staging.
Evidence delivery is bootstrap stage 16; Redis retains stage 15.
`deploy_staging_evidence.sh` checks that the shared cluster is `ACTIVE` before ECR lookups or
Kubernetes changes. It fails with an actionable message if it cannot verify the cluster or if the
cluster is not active; it never applies Terraform or creates the cluster automatically.

## Production gates that local success does not satisfy

Local Moto proves version, reference, and request-parameter contracts. It does not prove AWS IAM, KMS key policy, bucket versioning enforcement, private networking, or SQS authentication. The following are PR 5 gates and are not implemented or authorized here:

- Reviewed source, tool, and redaction policy for real data; `restricted-synthetic-v1` is a finite fixture policy, and production content mode is rejected at startup.
- Authenticated tenant and ACL guarantees. A signed `tenant_id` claim does not prove corpus ACL correctness, and retrieval ACL enforcement is not implemented.
- IAM, KMS, secret management, key rotation, and bucket lifecycle and versioning enforcement.
- Retention and deletion across every copy: queue, traces, backups, and datasets.
- Load and SLO validation, alerting on a metrics path that actually exists, migration and rollback procedures, and deployment ownership.

## Incomplete replay provenance

Stored sanitized model requests support future component replay, not necessarily identical replay of the original request. Cache hits can carry input and output slots but no model request, so generation replay is not possible for them, and the acceptance suite records the output slot as `unapproved_source` rather than inferring provenance from matching text. The acceptance graph exercises the approved direct-answer path; it does not certify every graph route or tool as approved, and only the calculator has an approved tool payload shape. Corpus, index, and ACL versions remain unknown where they were unknown in PR 3. Grading, replay execution, and the scoring consumer are later slices.
