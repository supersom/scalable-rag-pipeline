# PR 4: Safe Payload Storage and Durable Publication

**Status:** Written specification awaiting user review. The policy choices and three conversational design sections are approved; this document adds concrete contracts and proposed local limits for review. No PR 4 product code, dependencies, infrastructure, or production capture have been changed.

**Related documents:** [Gap 1 design](gap-01-production-run-instrumentation.md), [PR 4 planning draft](pr-04-payload-publication-plan.md), [local environment](local-environment.md).

## 1. Intent and scope

Complete the next instrumentation slice: turn PR 3's terminal, in-memory evidence into safe, durable records that separate workers can consume. Preserve the answer/streaming behavior and make evidence loss or incompleteness visible. Enable local verification without a GPU, real model provider, cloud deployment, or production credentials.

Production produces evidence; the evaluation plane interprets it. PR 4 implements storage and delivery, not grading, replay execution, release gates, human review, or Opik integration. Stored sanitized model requests enable future component replay; they are not necessarily identical to the original requests. Unknown corpus/index/ACL versions remain unknown, and this slice does not implement retrieval ACL enforcement.

Approved decisions:

- Transactional PostgreSQL outbox; durable delivery after commit, not audit-grade capture of every answer.
- Fail-open evidence admission: persistence failure never prevents returning an answer.
- Encrypted, redacted replay content; no sensitive raw-content tier.
- Preserve independently validated safe metadata and successfully stored safe payloads when other payloads fail.
- Upload payloads before committing the run record/outbox; no PostgreSQL prompt staging.
- Restricted, versioned field/source policies; no external DLP service in PR 4.
- Content expires 30 days after object creation; run metadata expires 90 days after run completion.
- Production content capture stays disabled pending security review and PR 5 controls.

The alternative queue-only delivery and encrypted database content-staging approaches are not implemented. A bounded API admission buffer is still needed to keep uploads off the response path; it is explicitly non-durable until the outbox transaction commits.

## 2. Existing boundaries and necessary integration

`evaluation/models.py` defines schema `1.0`, currently metadata-only. `context.py` captures hashes and observations and returns detached terminal snapshots. `routes/chat.py` finalizes success, error, timeout, cancellation, and header/body disconnect paths. Model, retrieval, and tool capture points already exist in `clients/ray_llm.py` and the corresponding agent nodes.

The collector snapshot and existing NDJSON response contract remain unchanged. Delivery uses a separate export representation and payload manifest. Do not insert content buffers into `AgentState` or run synchronous callbacks that secretly perform database/network IO.

The current JWT adapter returns user ID, role, and permissions, but no tenant. Extend it to expose an optional validated `tenant_id` from the already verified JWT. Missing or malformed tenant claims yield no capture tenant and do not change existing chat authorization semantics. Never accept a tenant from a body, arbitrary header, retrieved document, or model result. Signed claims alone do not prove corpus ACL correctness; that is a separate production gate.

Local acceptance fixtures use locally signed tokens with two synthetic tenants. No raw user ID or token is retained in export metadata. Tenant-scoped pseudonyms use a dedicated evidence identity key, not the JWT signing key.

## 3. Components and ownership

| Component | Responsibility | Boundary |
|---|---|---|
| `delivery_models.py` | Strict versioned export, payload, receipt, and policy contracts | Shared by capture, storage, and delivery; no network IO |
| `redaction.py` | Field/source eligibility, deterministic redaction, safe metadata projection | Produces validated redacted payloads, never arbitrary dictionaries |
| `payloads.py` | Versioned encrypted writes and authorized reads/deletion | Owns bounded S3 calls; no grading |
| `runtime.py` | API-owned capture budgets, terminal admission, finite upload/commit work | Lifespan-owned task group and bounded executor; no broker calls |
| `outbox.py`, `storage_models.py` | Immutable run/export persistence, upload intents, leases, retries | Short PostgreSQL transactions; independent of chat-history tables |
| `migrate.py`, SQL migrations | Version-tracked, transactional schema changes | Explicit startup/deployment command; never destructive recreation |
| `publisher.py` | Send committed metadata envelopes to SQS | Finite IO, stable event identity, no payload content |
| `services/evaluation_delivery/worker.py` | Claim, publish, acknowledge, retry/quarantine | Separate delivery service; not an evaluator or ingestion worker |
| Retention maintenance command | Authorized expiry and orphan cleanup | Uses the same repositories/store; explicit dry-run/apply modes |

The maintenance command belongs to the evaluation delivery package. Local orchestration may schedule it; production scheduling and cloud lifecycle enforcement belong to PR 5. No public payload resolver endpoint is added.

## 4. Data flow and durability boundary

1. With evidence disabled, do not create delivery buffers, clients, or background work. Preserve PR 3 behavior.
2. For an enabled accepted run, capture eligible content at request/model/retrieval/tool boundaries. Redact immediately into a bounded sidecar. Record fixed unavailable reasons for rejected payloads. Retain no additional raw copies.
3. Return/stream answer events without awaiting evidence admission, uploads, or outbox persistence. Capture/redaction is bounded local work, not an expensive judge or network operation.
4. Terminal finalization detaches the metadata snapshot and sanitized buffers once. `EvidenceRuntime.submit(...)` immediately returns `admitted`, `disabled`, `dropped`, or `failed`; `admitted` is not a durability promise. Header/body disconnect and generator completion share an idempotent admission guard.
5. A fixed API runtime worker takes admitted work. Validate the export projection; create a short-lived upload-intent lease before any S3 writes. Upload each eligible payload with a fixed deadline. Failed payloads become unavailable, while successful payload references are preserved.
6. Atomically insert the immutable safe run/export and pending outbox row, attach successful object references, and close the upload intent. Report `committed` only after the transaction succeeds. Release buffers on every outcome.
7. A standalone worker claims committed outbox rows and publishes their exact metadata envelope. Only after broker acknowledgement does it mark the row delivered.

The upload intent contains only run identity, generated object-key namespace, expiry, and state—not question text or staged content. It exists to prevent orphan cleanup from racing active uploads. It is not an evaluation event and does not satisfy the delivery guarantee. If intent creation fails, skip content writes and attempt safe metadata-only admission within the same bounded processing budget; a database outage will usually prevent that too.

Crashes before outbox commit can lose the run. Successful uploads followed by failed commits can leave orphan versions. After commit, worker restart recovers delivery, subject to configured quarantine and the approved 90-day metadata expiry. No atomic transaction spans S3 and PostgreSQL; no exactly-once broker claim is made.

## 5. Export and payload contracts

All new contracts are strict, reject unknown fields and non-finite numbers, and use UTC-aware timestamps. Proposed export version is `1.0`; its namespace is distinct from the existing run schema version.

`ExportEnvelope` contains:

- `export_version`, `run_schema_version`, `run_id`, `export_hash`, `redaction_policy_version`.
- A safe projection of `RunEvidence`, including approved configuration, observations, and operational metrics.
- An opaque tenant partition and export-safe session pseudonym, or null when unavailable.
- A payload manifest and fixed completeness state: `complete`, `partial`, or `metadata_only`.

`export_hash` is SHA-256 over canonical UTF-8 JSON excluding the hash field. Producer/consumer identity is `(export_version, run_schema_version, run_id)`; identical identity/hash duplicates are accepted, different hashes conflict. Any future envelope change requires explicit compatibility/version handling.

Each manifest entry has a fixed kind, positive ordinal, optional item ordinal, state (`stored` or `unavailable`), and either a verified `PayloadRef` or fixed reason code. Retrieval items, model requests/responses, and tool arguments/results are linked by ordinals, not by mutable list matching or payload text. A model logical call receives its ordinal at invocation start, even when calls complete out of order. Existing model-call arrays need an explicit ordinal mapping in the manifest, not a breaking schema `1.0` field.

`PayloadRef` contains configured bucket, generated key, non-empty version ID, opaque tenant partition, payload kind/ordinal, byte size, SHA-256 of the redacted serialized bytes, policy version, creation time, and expiry time. References are structured data, never public or presigned URLs. A failed upload or missing version ID never creates a usable reference.

Payload kinds and strict JSON shapes:

| Kind | Eligible fields |
|---|---|
| `input` | `text` |
| `output` | `text` |
| `retrieval_query` | `text` |
| `retrieval_chunk` | `text`; retrieval identity/rank stays in metadata |
| `model_request` | `messages: [{role, content}]`, `model`, `temperature`, `max_tokens` |
| `model_response` | `text` |
| `tool_arguments` | Per-tool approved shape, initially calculator `expression` only |
| `tool_result` | Per-tool approved shape, initially calculator `value` only |

Message roles are `system`, `user`, or `assistant`; content must be text. Unknown roles, multimodal parts, extra keys, and unsupported tools are unavailable rather than coerced into text. Capture only history actually sent to that model call. Do not fetch additional historical messages for evaluation. Record the actual outgoing model/decoding settings; do not pretend `json_mode` was sent when the existing client does not send it.

Successful model text may be stored; error response bodies and exception messages may not. For calculator capture, the input must match the bounded numeric-expression grammar used by the capture policy (digits, whitespace, arithmetic punctuation, and explicitly listed `sqrt`); the result must be a finite numeric text value. Error strings, unsupported expressions, sandbox code/results, web-search results, and graph-search tool IO are unavailable in the initial policy. Tests may add a dedicated synthetic tool adapter, but that does not approve production tools.

Completeness describes the observed run's eligible payload slots, not task success. Disabled capture is `metadata_only`. Any denied, failed, oversized, or missing required slot makes a content-captured run `partial`. Cache hits can have input/output slots but no model request; do not invent model calls or promise generation replay for them. Interrupted runs do not invent an answer slot.

## 6. Restricted policy and provenance

Initial policy ID: `restricted-synthetic-v1`. It is valid only with explicitly selected local synthetic fixtures and emulator/fake-model endpoints. Production content mode is rejected in this slice; metadata delivery can be enabled separately with valid configuration.

Local source approval is an explicit fixture manifest with synthetic tenant ownership, input/history cases, model template hashes, corpus collection/document identifiers, and source hashes. Approval is not an arbitrary document field saying `safe=true`. Mixed, absent, or mismatched source provenance denies the affected content. A fixture case must be selected through trusted local test configuration, not a user-controlled body. This deliberately restricts local acceptance tests; it is not a general-purpose production sanitizer.

At context assembly, maintain sidecar provenance describing which approved history, corpus chunks, tool outputs, and application templates contributed to the model messages. Flattening cannot convert an unapproved source into an approved prompt. If any contributing source is unapproved, omit the affected model request and derived response/output content. Keep independently safe input and trajectory metadata. Citation filenames embedded in prompts are content and must be approved synthetic values or denied; standalone filename/path fields are not exported.

Redaction is deterministic and versioned:

- Deny structural credential fields after case/separator normalization: authorization, cookie/set-cookie, password, secret, token/access-token/refresh-token, api-key, and private-key. These are never part of an eligible payload schema.
- Replace PEM private-key blocks with `[REDACTED_PRIVATE_KEY]`; an unmatched opening private-key marker denies the payload.
- Replace tested bearer credentials, three-segment JWT-like tokens, AWS access-key-ID patterns, and explicit password/token/api-key assignments with `[REDACTED_SECRET]`.
- Replace bounded email-address patterns with `[REDACTED_EMAIL]` and international phone patterns beginning with `+` and containing 8–15 digits with `[REDACTED_PHONE]`. Do not claim detection of all international/local phone formats.
- Apply secret/private-key rules before email/phone rules, deterministically handling overlaps. Rules process only the supported text fields; nested unknown structures are denied, not recursively exported.

Exact pattern definitions and literal expected outputs are versioned together in policy code and fixtures. They must cover single/multiline, mixed-case assignment labels, nested forbidden fields, overlapping matches, and malformed delimiters. Synthetic fixtures demonstrate this finite behavior, not safety of arbitrary production text. Do not redact the answer sent to users; only the evidence copy is transformed.

Fixed payload reason codes: `capture_disabled`, `missing_tenant`, `unapproved_source`, `unapproved_tool`, `unsupported_shape`, `redaction_failed`, `payload_too_large`, `run_budget_exceeded`, `payload_count_exceeded`, `upload_failed`, `upload_timeout`, `not_observed`. Reasons never include offending content or exception messages.

## 7. Safe metadata and identity

Do not serialize `RunEvidence` wholesale merely because it is called metadata. Export numeric/boolean fields and closed status enums after validation. For string fields:

- Server-generated run IDs must be UUIDs; trace IDs must be fixed-format hex. Export only validated forms.
- Tenant/session/document/chunk identifiers and provider request/completion IDs are tenant-scoped HMAC pseudonyms. Invalid or unavailable identifiers become null with a fixed reason. Use separate domain labels for each identifier kind.
- Existing user HMAC may be exported only when derived with the configured dedicated key and trusted tenant; otherwise null.
- Model/collection/build/version/purpose/tool/dependency names must match configured application allowlists or fixed validated build/hash formats. Unrecognized strings become unavailable, not loggable diagnostics.
- Preserve only typed dependency attributes already allowed by the collector, with per-key value validation. Unknown attributes are omitted; known keys with invalid values do not bypass validation.
- Error names are mapped to a finite application-approved class set plus `OtherError`; never export exception messages.
- Preserve approved configuration hashes/fingerprint without claiming absent provenance. Fingerprints are hashes of application configuration, not content.

The detached original snapshot retains its existing hashes. Export source-content hashes as domain-separated tenant HMACs of the original digest to reduce low-entropy dictionary exposure; the payload reference's redacted-byte SHA-256 is a separate integrity value. Never substitute a redacted hash and label it the original hash. Missing tenant/key means original-content hashes are unavailable in export, not plaintext fallbacks.

Enabled metadata delivery requires a dedicated identity key; startup rejects accidental reuse of the JWT secret. This key is sensitive runtime configuration, never a dataset/artifact or log field. Key rotation and production secret management are PR 5 operational controls.

## 8. Payload storage and authorized access

Write only the validated redacted type. Keys are generated from configured environment, opaque tenant partition, UTC date, server run UUID, fixed kind, and validated ordinals. Reject path traversal and unexpected key segments. Buckets/keys are never derived from user text.

S3 requests specify `ServerSideEncryption="aws:kms"` and a configured dedicated key. Local Moto round trips exercise version/reference contracts, not actual KMS encryption or IAM authorization. Stubber tests assert production request parameters. Production bucket versioning, key permissions, encryption enforcement, and private networking are staging gates.

The resolver requires a trusted authorized tenant, verifies tenant partition, configured destination/prefix, policy/version, and expiry before IO, then reads the exact version. It verifies byte limits and hash before returning bytes. A caller-supplied reference cannot select another bucket, tenant, key namespace, or expired version. Missing/mismatched tenant is denied before object reads.

Owned boto3 clients use a dedicated bounded executor, finite socket timeouts, and finite retries. Await cancellation does not free a worker slot while its underlying blocking operation still runs. Do not use unbounded `to_thread`/task creation; shutdown stops new work and bounds draining. Residual bounded SDK calls are explicitly accounted for, not described as forcibly cancelled threads.

## 9. PostgreSQL persistence and publication

Use separate evidence tables/migrations; never reuse chat history as an outbox. The immutable run row stores canonical safe export bytes/hash and expiry. The mutable outbox row stores identity, state, attempts, next attempt, lease token/expiry, acknowledgement ID, and safe error class. Upload-intent rows contain bounded coordination metadata only. Enabled metadata-only delivery requires PostgreSQL, queue configuration, and the identity key; content capture additionally requires the synthetic policy/fixture manifest, versioned bucket, dedicated KMS request key, and bounded storage configuration. Disabled content does not require an S3 client.

Atomic insertion covers run/outbox/references and intent closure. A matching duplicate returns the original committed receipt; a conflicting duplicate never overwrites evidence. Use version-tracked transactional migrations with a database-level migration lock and checksum validation. Local startup runs migrations before API and worker; production deploy workflow is separate. Do not use `create_all` as schema evolution or drop/recreate tables.

Workers claim short batches with row locking and expiring random lease tokens. Never hold locks during SQS calls. Every retry/acknowledgement update checks the current lease token; expired/stale claims cannot overwrite a newer claim. If acknowledgement succeeded but its database update failed, a later claim sends the same bytes/identity/hash again.

The worker validates envelope hash, size, and metadata expiry before sending. Payload expiry does not justify changing the immutable envelope/hash; consumers resolve expired references as unavailable. SQS standard delivery is treated as at-least-once. A test consumer transactionally stores identity/hash: identical redelivery succeeds once, conflicting redelivery is quarantined. The real scoring consumer belongs to a later slice.

Retry transient failures with bounded exponential backoff; quarantine permanent configuration/authorization failures and exhausted retries. Operator retry preserves identity/hash and requires an explicit command. Producer quarantine is not the SQS consumer DLQ. Expired records are never replayed or republished.

## 10. Proposed local resource defaults

These are engineering defaults for written-spec review, not new production SLOs. Configuration must reject non-positive or inconsistent values. Production tuning requires load tests.

| Limit | Initial default |
|---|---|
| Serialized redacted payload | 64 KiB |
| Content per run / payload slots | 1 MiB / 64 |
| Active content buffers | 32 runs / 16 MiB combined |
| Pending terminal admissions | 32 runs / 16 MiB combined |
| Upload/commit runtime workers | 2 |
| S3 executor concurrency | 2; no unbounded backlog |
| Payload upload/commit admission deadline | 20 seconds total; reserve final 3 seconds for metadata commit |
| SDK connect/read timeouts / total attempts | 1 second / 2 seconds / 2 attempts |
| Queue metadata body | 128 KiB UTF-8 |
| Delivery worker batch / send concurrency | 10 / 2 |
| Outbox claim lease | 60 seconds |
| Producer attempts / backoff | 8 attempts; 1-second base, 60-second cap, bounded jitter |
| Shutdown drain | 10 seconds |
| Upload-intent lease / orphan grace | 60 seconds / 24 hours |

Each active/pending pool has an independent byte/run cap; transfers do not duplicate content buffers. When no capture slot is available, continue metadata-only collection and expose a fixed omission reason. Admission overflow drops that terminal export and increments loss metrics. Do not silently truncate replay content or fabricate full completeness. An oversized metadata envelope is rejected with a measured loss, not sent as a truncated record. Serialization rejects excessive nesting before recursive processing.

Deadlines include scheduling and retries, not just individual socket calls. An expired upload job cannot later commit after its intent has expired. Safe metadata admission after failed content stays within its final reserved deadline. Local tests prove budgets and thread/task counts remain bounded under hung SDK calls.

## 11. Retention and cleanup

Store explicit UTC expiry timestamps. Authorized access refuses expired content even if physical deletion is delayed. Content expiry is object creation plus 30 days; run expiry is run completion plus 90 days. Duplicate admission does not extend either expiry.

Maintenance enumerates only the configured evidence bucket/prefix and exact evidence tables. Delete expired object versions and delete markers, not merely create a new delete marker. Metadata deletion removes the associated run/outbox/reference records at 90 days, including quarantined delivery metadata; retention takes precedence over retry. Consumers reject expired run metadata. Queue copies, trace stores, backups, and dataset retention are separate policies, not implicitly granted another 90 days.

Orphan cleanup deletes only versions older than 24 hours with no committed live reference and no live upload intent. It checks PostgreSQL availability and atomically marks the expired intent namespace as cleanup-owned under a short transaction; the commit path rejects expired/closed/cleanup-owned intents, preventing reference insertion after cleanup eligibility. For a namespace without an intent, cleanup inserts the same exclusion marker before deletion. Delete objects outside the transaction, then record completion; restart can resume incomplete cleanup. If coordination cannot be established, defer deletion and emit a cleanup-failure metric. Expired coordination rows can be removed after their objects are resolved; they do not extend content retention.

Deletion is idempotent, paginated, and bounded. Dry-run lists safe counts/identities, never payload text. Local tests cover multiple versions, delete markers, failed deletion, retry, active-intent races, database outage, and expired resolver access. Physical production deletion/backups/legal requirements remain staging/organizational gates; local success is not a compliance claim.

## 12. Observability and local acceptance

Counters distinguish capture omissions, admission attempts/drops, committed exports, upload failures, publish retries, delivered records, quarantine, and cleanup failures. Gauges expose pending count/bytes, oldest pending age, and quarantine count/age. Histograms cover evidence processing/upload/publish latency and payload bytes. Labels use only fixed component/outcome/reason enums—never run/user/tenant IDs, paths, exception messages, or queue bodies. Evidence failures never change the original agent outcome.

Local stack additions: explicit Moto SQS support, evaluation queue/DLQ initializer, migration startup step, and standalone delivery worker. Keep loopback-only host ports, dummy AWS credentials, fake models, and existing ingestion resources untouched. An opt-in synthetic capture profile supplies fixture policy and dedicated local identity/encryption settings; default configuration still disables evidence delivery/content. No API keys are needed for fake models or emulated AWS.

Required automated verification:

1. Strict contracts, disabled parity, invalid enabled configuration, literal redactions, forbidden fields, unapproved sources/tools, missing tenant, and provenance propagation through flattened prompts.
2. S3 versioned round trips, exact KMS request parameters, wrong-tenant denial before IO, hash mismatch, oversized/unsupported payloads, partial upload, and finite SDK waits.
3. Real PostgreSQL atomic insertion/rollback, duplicate/conflict behavior, migrations/reapplication/concurrent migration startup, upload intents, two dispatchers, stale claims, and restart recovery.
4. Normal/cache/error/timeout/cancelled/header/body-disconnect paths submit once; run/schema snapshots and NDJSON remain compatible. Shutdown/overflow/hung IO remain bounded and do not block answer delivery.
5. Broker outage, acknowledgement-before-mark crash, identical redelivery, consumer conflict, quarantine, expiry, and version/orphan cleanup races.
6. Live local signed synthetic runs join run IDs, PostgreSQL rows, queue envelopes, and object versions; demonstrate safe complete/partial/metadata-only runs and worker restart.
7. Full pytest, focused observability tests, pinned API-container tests, `make local-check`, `make local-smoke`, opt-in delivery acceptance tests, and whitespace checks. Record actual results when implemented; this specification has not run new runtime tests.

## 13. Completion and handoff

PR 4 is complete only when approved synthetic component-replay payloads are actually stored and linked, committed records recover through publication, and failure/privacy/resource/retention tests pass. Metadata-only delivery is useful but does not complete the selected replay scope. Retain one inspectable green commit per functionality module, then acceptance/orchestration/docs boundaries; no intentionally red commits land.

Production enablement is a separate PR 5 gate: reviewed source/tool/redaction policy, authenticated tenant/ACL guarantees, IAM/KMS/secret controls, retention/deletion across all copies, load/SLO validation, migration/rollback procedures, and deployment ownership. No cloud provisioning or production capture is authorized here.

After user review of this written specification, create a single-path implementation plan using the approved contracts. The existing PR 4 planning draft remains non-executable until revised and separately reviewed; remove unselected alternatives and align interfaces with non-durable `admitted` versus durable `committed` receipts. Do not start implementation merely because the conversational design was approved.
