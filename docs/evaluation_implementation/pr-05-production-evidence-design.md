# PR 5: Production Evidence Delivery Activation

**Status:** Written specification approved by the user in conversational design. Implementation-plan review and execution-method selection are still required for PR 5a. PR 5b is designed here but is explicitly not scheduled for implementation; it is blocked on a prerequisite outside this spec's scope (see [5b prerequisite](#pr-5b-content-capture-enablement)). No PR 5 infrastructure, dependency, or production change has been made.

**Related documents:** [PR 4 design](pr-04-payload-publication-design.md), [PR 4 operations](evidence-delivery-operations.md), [Gap 1 progress](gap-01-production-run-instrumentation.md), [evaluation-plane gap analysis](../evaluation-plane-gap-analysis.md).

## 1. Intent and scope

PR 4 built and locally verified redacted evidence storage and durable delivery, entirely on synthetic fixtures, fake models, and Moto. Every local claim about IAM, KMS enforcement, private networking, queue authentication, and production content safety was explicitly deferred to PR 5. This document splits that deferred work into two parts:

- **PR 5a** takes metadata-only delivery from local-only to running safely in production: real staging validation, real IAM/KMS, a working metrics path, a drain consumer that closes the "nothing consumes the queue yet" gap, and a rollout/rollback procedure. This part is scheduled for implementation next.
- **PR 5b** defines what would need to change to safely capture real production content instead of synthetic fixtures: hardened redaction, a production content policy, consent and retention implications. This part is designed now, so the redaction and provenance work has a documented target, but it is **not scheduled**. It is blocked on retrieval ACL enforcement, which does not exist yet and is out of scope here.

**Non-goals:** building the Phase 3 evaluation workers that will eventually be the real consumer of `eval-runs` (5a's drain consumer is a deliberate placeholder, not that worker); building retrieval ACL enforcement; revisiting any PR 4 contract (export schema, outbox semantics, the 30/90-day retention windows) — those are fixed inputs here.

## 2. Staging environment

A new, separate Terraform state — not a workspace on the existing `platform/terraform.tfstate` — so a mistake here cannot corrupt production state. New directory `infra/terraform-staging/`, backend key `platform/staging-evidence.tfstate`, same S3 state bucket and region as `infra/terraform/main.tf`.

It reads the existing cluster and VPC by data lookup (`data "aws_eks_cluster"`, `data "aws_vpc"`, matched by the existing `cluster_name`/tags) rather than duplicating them, and creates only what evidence delivery needs, every resource named and tagged `-staging-evidence`:

- **Database:** `db_tier = "rds"` — `db.t3.micro`, no HA, matching the repository's own dev/test default in `terraform.tfvars.example`. Staging only needs to prove PostgreSQL transaction semantics (advisory-lock migrations, atomic outbox commit, lease-token claims), none of which depends on Aurora's autoscaling or multi-AZ properties, so the cheaper tier is correct here even though production `chat_history` uses Aurora.
- **S3** bucket with versioning and a 30-day lifecycle rule, plus its own KMS key.
- **SQS** queue and DLQ with a redrive policy.
- **IAM:** one IRSA role per component (API, delivery worker, drain consumer, maintenance CronJob), least-privilege, following the same `terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks` pattern already used for the ingestion worker in `infra/terraform/iam.tf`.
- A `staging-evidence` Kubernetes namespace, with a Helm release of the API pointed at these resources: `EVAL_EVIDENCE_ENABLED=true`, `EVAL_CAPTURE_CONTENT=false`.

No GPU nodes, no Ray, no dedicated Qdrant/Neo4j — the chat endpoint's answer quality is not under test here, so the staging API talks to a lightweight `services/fake_models` deployment, the same fake models used locally, rather than paying for real inference. This keeps the environment cheap enough to stand up for a validation run and tear down, or leave running — that operational choice is made at build time, not here.

## 3. PR 5a: production activation

### IAM and KMS

One dedicated KMS key for the evidence bucket. Its key policy grants `Encrypt`/`Decrypt`/`GenerateDataKey` only to the four IRSA roles below, not an account-wide grant. The bucket policy denies any `PutObject` that does not request `aws:kms` with that key, enforcing server-side what PR 4 already sends client-side.

### Deployment ownership

| Component | Kubernetes shape | IRSA role |
|---|---|---|
| `evidence-migrate` | Helm pre-upgrade hook Job | dedicated |
| Delivery worker | Deployment, 1–2 replicas | dedicated |
| Drain consumer (below) | Deployment | dedicated |
| Retention maintenance | CronJob, daily `cleanup --apply --limit <N>` | dedicated |

`N` for the maintenance CronJob is sized from a staging dry-run before it is ever set in production; this document does not fix a number.

### The drain consumer

Phase 3, the real evaluation-worker consumer of `eval-runs`, does not exist yet. Enabling production delivery without any consumer would mean committed evidence sits in SQS until it ages out — silently defeating the point of durable delivery. PR 5a adds a minimal drain consumer instead of waiting for Phase 3 or accepting the loss:

- Claims batches from `eval-runs`, validates envelope hash and size against the SQS body (mirroring the consumer contract in the PR 4 design).
- Writes each envelope to `s3://<evidence-bucket>/queue-archive/<run_id>.json`, keyed by `run_id`. Because the key is the run ID, identical redelivery is a no-op overwrite — dedup without a new database.
- A **conflicting** hash for the same `run_id` (corruption, not redelivery) is written to `queue-archive-conflict/` instead of overwriting, and increments a counter — this path must never silently discard a hash mismatch.
- Acknowledges only after the write succeeds.

When Phase 3's real evaluation-worker consumer exists, it replaces this consumer or replays from `queue-archive/`; this component is explicitly a placeholder, not a scorer.

### Observability

Three fixes close the gaps found in PR 4 local verification:

1. `services/evaluation_delivery/worker.py` and `maintenance.py` construct `default_metrics()` instead of passing none, so `publisher`, `worker`, `cleanup`, and `maintenance` events are recorded outside tests.
2. The worker polls quarantine count and oldest-age once per claim cycle and calls `DeliveryMetrics.quarantine(...)`, which today has no caller.
3. The API gets a `/metrics` route serving the existing Prometheus registry (closes the pre-existing "API `/metrics` endpoint not wired" backlog item), scraped in-cluster only via a PodMonitor, consistent with the already-deployed kube-prometheus-stack (`deploy/monitoring/ray-service-monitors.yaml`).

The maintenance CronJob is short-lived and not a natural Prometheus scrape target; rather than add a Pushgateway dependency, alert on its fixed-key stdout line and exit code, which it already emits.

### Rollout and rollback

Dark-launch: deploy the worker, drain consumer, and CronJob to production with `EVAL_EVIDENCE_ENABLED=false` first, so the deploy itself changes no behavior. Flip the flag in a separate, scoped configuration change only after the staging validation in [Section 5](#5-validation-plan) passes, and watch `evidence_pending_runs`, `evidence_quarantine_records`, and the drain consumer's conflict counter through a bake period (duration is an operational call at rollout time, not fixed here).

Disabling the flag is an instant, safe rollback — disabled skips every new code path, fail-open by construction. Rolling back a **bad migration** is not solved by this design: `services/api/app/evaluation/migrate.py` is forward-only with no down-migration support, consistent with PR 4's "never drop or recreate" stance. A bad migration needs a forward-fix migration, not a revert. This is an accepted risk, not a gap this PR closes.

## 4. PR 5b: content-capture enablement

**Not scheduled.** Designed so the redaction and provenance work has a documented target; blocked on retrieval ACL enforcement, which does not exist today (any tenant's query can retrieve any document) and is out of scope for this document. 5b must not ship before that dependency is resolved, wherever it gets its own spec.

### Redaction hardening

The independent PR 4 review measured concrete gaps in `services/api/app/evaluation/redaction.py`: JSON-quoted secret values (`"password": "..."`), CLI-flag style (`--password`, `--token`), the `passwd`/`pwd` spelling, `Authorization: Basic`/`Cookie:` headers, credentials embedded in URLs, and `sk-`/`ghp_`-style provider keys all pass through unredacted; a `token:abc def` value is only partially redacted. 5b fixes these directly, in the same finite-pattern-set style as PR 4 — no new dependency, no NER-based detector. This closes known holes; it does not claim to catch every possible secret shape in arbitrary text, and the operations doc must keep saying so.

### A new content policy

`restricted-synthetic-v1`'s approval model — a payload's hash must match a checked-in fixture — is meaningless for arbitrary production text. 5b defines `production-redacted-v1`:

- No fixture-manifest hash match required.
- Every payload still passes through the hardened redaction patterns above before storage.
- Provenance still gates derived content exactly as today: an unapproved or unflattened source still denies the request/response/output slots.
- `EVAL_CONTENT_POLICY`'s conditional validation in `delivery_config.py` gains this policy value, and the `ENV` gate for content capture changes from `local|test` to an explicit allowlist that includes `prod` — but only reachable after the security review below, never as a config change made unilaterally.

### Prerequisite: retrieval ACL enforcement

Today any tenant's query can retrieve any document; there is no per-tenant retrieval isolation. Capturing real production content under that condition would durably store evidence of a system with no actual tenant isolation, and does not become safer by being redacted. 5b's activation is blocked on retrieval ACL enforcement existing and being verified, wherever that work is specified — it is not designed in this document.

### Consent and progressive enablement

Unlike 5a's single global flag, content capture should launch per-tenant against an explicit allowlist in configuration, mirroring 5a's dark-launch pattern — except the "bake period" here is legal and product sign-off per tenant, not an error-rate watch. Which tenants, and under what consent terms, is a product and legal decision to make before implementation, not one resolved by this spec.

### Retention beyond PR 4's 30/90-day windows

- Aurora automated backups retain the redacted envelope bytes for their own backup-retention window. This must be stated explicitly wherever 5b is documented for operators, not assumed to match the 90-day metadata window.
- OTel traces stay hash-only per the PR 3 design, so they carry no new content risk from 5b.
- The real gap is downstream: once Phase 3/4 build evaluation datasets *from* production evidence, a derived dataset (e.g., a checked-in JSONL) that embeds raw payload text can outlive the 30-day content window entirely if nothing gates it. 5b states this as a hard requirement for whoever builds that later: derived datasets must carry their own redaction, retention, and consent, never inherit safety from the source record's expiry. The mechanism for that is not designed here.

## 5. Validation plan

**Staging, before touching production.** Apply `infra/terraform-staging/`, deploy the staging Helm release with evidence enabled, run migrations via the hook Job. Then repeat the checks already proven locally in PR 4's Task 11 against a disposable Compose project — a signed synthetic JWT through `/chat/stream`; the outbox row reaching `delivered`; the maintenance CLI's dry-run, apply, unknown-run retry, and invalid-argument cases; killing the worker pod mid-delivery and confirming restart recovery; confirming a `queue-archive/` object appears with the correct hash. This is the first time any of this runs against real IAM/KMS/SQS, so additionally confirm the KMS key policy actually rejects an unauthorized role, and the bucket policy actually rejects a non-KMS put — negative tests that Moto cannot exercise.

**Production, after staging passes.** Dark-launch per Section 3, then flip the flag and watch the metrics named there through a bake period before calling 5a complete.

## 6. Cost

Staging adds one `db.t3.micro`, one small S3 bucket, one SQS queue pair, one KMS key, and a handful of small pods — on the order of the existing `~$15/month` RDS dev tier plus negligible SQS/S3/KMS charges. Production 5a adds a KMS key and the worker/drain-consumer/CronJob pods; no new database (reuses the existing outbox tables) and no GPU. Small relative to the GPU/Ray costs already in the cluster, but every `terraform apply` here still needs explicit go-ahead before it runs, per standing practice — this document authorizes design, not infrastructure changes.

## 7. Completion and handoff

PR 5a is complete when staging validation (Section 5) passes, production is dark-launched and then activated with a clean bake period, and the drain consumer, metrics, and CronJob are all operating as designed. PR 5b remains explicitly unscheduled pending the ACL prerequisite; this document exists so that dependency is visible and its target design does not need to be re-derived later.

After user review of this written specification, create a single-path implementation plan for **PR 5a only**. PR 5b is not planned or implemented from this document without a separate approval once its prerequisite is resolved.
