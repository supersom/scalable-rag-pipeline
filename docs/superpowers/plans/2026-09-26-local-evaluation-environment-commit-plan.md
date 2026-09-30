# Local Evaluation Environment Commit Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to package and verify these existing changes commit-by-commit. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the completed local evaluation-environment work into small commits, with one commit per functionality module and no unrelated files.

**Architecture:** Each of the three independent modules follows a red→green test cycle in the working tree, then lands as one green commit containing its tests and implementation. Overall acceptance tests and Docker Compose orchestration follow the same pattern and land together. Two documentation commits finish the six-commit series.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Moto, boto3, OpenTelemetry Collector, Docker Compose, Make, pytest.

**Spec:** `docs/evaluation_implementation/gap-01-production-run-instrumentation.md`

## Global constraints

- Stage only the paths listed for each commit. Never use `git add .` or `git add -A`.
- Preserve the unrelated untracked documentation listed under "Explicit exclusions."
- For each module, run and record the red test state before staging, implement the module, rerun to green, and then commit the test and implementation together.
- Every landed commit must pass its focused tests. No intentionally red commit may enter repository history.
- Do not commit real credentials. `.env.evidence` contains local-only placeholders.
- Keep every published development port bound to `127.0.0.1`.
- Keep production-run instrumentation out of this series. This series prepares the local environment; Gap 1 PR 1 implements tracing and run identity.
- Do not push unless the user requests it.

## Review focus

- A developer with standard database ports already occupied should still be able to start the isolated stack.
- The fake planner must return valid routing JSON for the smoke request, without invoking retrieval.
- The S3 emulator must not report healthy until `eval-traces` exists with versioning enabled.
- The API container must use Compose DNS names while host-side tools use loopback endpoints.
- The smoke test must fail on API error events or a missing deterministic answer, not merely on HTTP failure.

## Explicit exclusions

Do not stage these unrelated untracked documents in this series:

- `docs/code-walkthrough.md`
- `docs/helm-charts-notes.md`
- `docs/kubernetes-notes.md`
- `docs/kubernetes-service-conventions.md`

`docs/evaluation-plane-gap-analysis.md` is handled deliberately in Commit 6 because the Gap 1 implementation plan links to it. If that earlier analysis is not intended to land, omit Commit 6 and leave both linked Gap 1 documents uncommitted rather than creating a broken link.

---

### Commit 1: Deterministic local model service

**Commit message:** `feat(local-eval): add deterministic model service`

**Files:**

- Create: `tests/local_env/test_fake_models.py`
- Create: `services/fake_models/__init__.py`
- Create: `services/fake_models/app.py`
- Create: `services/fake_models/Dockerfile`
- Create: `services/fake_models/requirements.txt`

**Responsibility:** Specify and implement the CPU-only chat and embedding substitute, including OpenAI-compatible metadata, valid planner JSON, deterministic 1,024-dimensional embeddings, prefixed-greeting routing, health reporting, and provider failure simulation.

**Interfaces:**

- Produces: `GET /health`, `POST /v1/chat/completions`, and `POST /api/embeddings`.
- Produces: container port `8000`, later published on host port `8001` by Compose.
- Consumed by: the API client configuration and acceptance tests in Commit 4.

- [ ] Before staging, run the test without the module and record the red state:

  ```bash
  pytest tests/local_env/test_fake_models.py -q
  ```

  Expected: failures report that `services.fake_models.app` is missing.

- [ ] Implement the module, then verify the focused suite turns green:

  ```bash
  pytest tests/local_env/test_fake_models.py -q
  python -m compileall -q services/fake_models
  ```

  Expected: 5 passed and compilation exits successfully.

- [ ] Stage the specification and implementation together, then commit the green unit:

  ```bash
  git add tests/local_env/test_fake_models.py services/fake_models
  git diff --cached --stat
  git diff --cached --check
  git commit -m "feat(local-eval): add deterministic model service"
  ```

---

### Commit 2: Versioned local S3 evidence store

**Commit message:** `feat(local-eval): add versioned S3 emulator`

**Files:**

- Create: `tests/local_env/test_aws_emulator.py`
- Create: `services/aws_emulator/__init__.py`
- Create: `services/aws_emulator/init_s3.py`
- Create: `services/aws_emulator/healthcheck.py`
- Create: `services/aws_emulator/entrypoint.sh`
- Create: `services/aws_emulator/Dockerfile`
- Create: `services/aws_emulator/requirements.txt`

**Responsibility:** Specify and implement the Moto S3 lifecycle, `eval-traces` bucket creation, versioning, bounded readiness retry, error propagation, and health contract.

**Interfaces:**

- Produces: an S3-compatible endpoint on container port `4566`.
- Produces: a versioned bucket named by `EVAL_EVIDENCE_BUCKET`.
- Consumed by: Compose health checks and the acceptance tests in Commit 4.

- [ ] Before staging, run the test without the module and record the red state:

  ```bash
  pytest tests/local_env/test_aws_emulator.py -q
  ```

  Expected: collection fails because `services.aws_emulator` does not exist yet.

- [ ] Implement the module, then verify the focused suite turns green:

  ```bash
  pytest tests/local_env/test_aws_emulator.py -q
  python -m compileall -q services/aws_emulator
  sh -n services/aws_emulator/entrypoint.sh
  ```

  Expected: 8 passed and both static checks exit successfully.

- [ ] Stage the specification and implementation together, then commit the green unit:

  ```bash
  git add tests/local_env/test_aws_emulator.py services/aws_emulator
  git diff --cached --stat
  git diff --cached --check
  git commit -m "feat(local-eval): add versioned S3 emulator"
  ```

---

### Commit 3: Local OpenTelemetry Collector

**Commit message:** `feat(observability): add local OTLP collector`

**Files:**

- Create: `tests/local_env/test_otel_collector.py`
- Create: `deploy/otel/collector.local.yaml`

**Responsibility:** Specify and configure OTLP gRPC/HTTP ingestion, Collector health, memory limiting, batching, debug export, and successful live span delivery.

**Interfaces:**

- Produces: OTLP gRPC on container port `4317`, OTLP HTTP on `4318`, and health on `13133`.
- Consumed by: the Compose service and acceptance tests in Commit 4.

- [ ] Before staging, run the test without the Collector configuration and record the red state:

  ```bash
  pytest tests/local_env/test_otel_collector.py -q
  ```

  Expected: the two configuration tests fail because `deploy/otel/collector.local.yaml` is missing; the live test is skipped.

- [ ] Add the Collector configuration, then verify the non-live tests turn green:

  ```bash
  pytest tests/local_env/test_otel_collector.py -q
  ```

  Expected: 2 passed, 1 skipped. The live export remains skipped until Commit 4 supplies the running stack.

- [ ] Stage the specification and configuration together, then commit the green unit:

  ```bash
  git add tests/local_env/test_otel_collector.py deploy/otel/collector.local.yaml
  git diff --cached --stat
  git diff --cached --check
  git commit -m "feat(observability): add local OTLP collector"
  ```

---

### Commit 4: Local orchestration and acceptance

**Commit message:** `feat(local-eval): add isolated evaluation stack`

**Files:**

- Create: `scripts/smoke_local_env.py`
- Create: `tests/local_env/test_local_configuration.py`
- Create: `tests/local_env/test_running_stack.py`
- Create: `tests/test_pytest_configuration.py`
- Create: `pytest.ini`
- Create: `requirements-loadtest.txt`
- Modify: `.env.evidence`
- Modify: `docker-compose.yml`
- Modify: `Makefile`

**Responsibility:** Specify and implement the integration of the three modules with the containerized API, PostgreSQL, Redis, Qdrant, and Neo4j. Own local defaults, loopback-only ports, dependency ordering, developer commands, authenticated end-to-end acceptance, pytest discovery boundaries, and the optional load-test dependency.

**Interfaces:**

- Consumes: fake model port `8000`, S3 port `4566`, and Collector ports `4317`, `4318`, and `13133`.
- Produces: `make local-up`, `make local-down`, `make local-check`, and `make local-smoke`.
- Produces: `make load-test-install` and `make load-test`, while retaining the representative `scripts/load_test.py` filename.
- Produces: host endpoints on ports `18000`, `8001`, `15432`, `16379`, `16333`, `17474`, `17687`, `14566`, `14317`, `14318`, and `13134`, all on `127.0.0.1`.

- [ ] Before staging, run the acceptance tests without orchestration and record the red state:

  ```bash
  pytest tests/local_env/test_local_configuration.py tests/local_env/test_running_stack.py -q
  ```

  Expected: local configuration assertions fail because the `eval-local` profile and settings are absent; the live running-stack test is skipped.

- [ ] Add orchestration, then verify the non-live acceptance tests turn green:

  ```bash
  make local-check
  ```

  Expected: 18 passed, 2 skipped. The skipped tests are the opt-in running-stack and OTLP export tests.

- [ ] Before adding pytest discovery configuration, run its regression test and record the red state:

  ```bash
  pytest tests/test_pytest_configuration.py -q
  ```

  Expected: the nested bare pytest command discovers `scripts/load_test.py` and fails because optional Locust is not part of the correctness-test environment.

- [ ] Add `pytest.ini`, pin Locust separately, and verify discovery and load-test commands:

  ```bash
  pytest tests/test_pytest_configuration.py -q
  make -n load-test-install
  make -n load-test
  ```

  Expected: the regression test passes, and both Make commands resolve without running or installing anything.

- [ ] Build and start the complete stack:

  ```bash
  make local-up
  ```

  Expected: all eight services start; API, fake models, PostgreSQL, Redis, S3, and the Collector report ready or healthy.

- [ ] Verify all live acceptance behavior:

  ```bash
  make local-smoke
  pytest -q
  ```

  Expected: the smoke suite reports 4 passed; bare pytest reports 158 passed, 2 skipped without collecting the Locust scenario.

- [ ] Stage the acceptance specification and orchestration together, then commit the green unit:

  ```bash
  git add scripts/smoke_local_env.py tests/local_env/test_local_configuration.py tests/local_env/test_running_stack.py tests/test_pytest_configuration.py pytest.ini requirements-loadtest.txt .env.evidence docker-compose.yml Makefile
  git diff --cached --stat
  git diff --cached --check
  git commit -m "feat(local-eval): add isolated evaluation stack"
  ```

---

### Commit 5: Local environment operator guide

**Commit message:** `docs(local-eval): document local evaluation environment`

**Files:**

- Create: `docs/evaluation_implementation/local-environment.md`
- Create: `docs/superpowers/plans/2026-09-26-local-evaluation-environment-commit-plan.md`

**Responsibility:** Explain how to start, verify, troubleshoot, customize, and stop the local environment. Preserve this commit plan for review and reproducibility.

- [ ] Check the documented commands and links:

  ```bash
  test -f .env.evidence
  test -f docs/evaluation_implementation/local-environment.md
  rg -n "make local-check|make local-up|make local-smoke|make local-down|make load-test" docs/evaluation_implementation/local-environment.md
  ```

- [ ] Stage and inspect only the local operator documentation:

  ```bash
  git add docs/evaluation_implementation/local-environment.md docs/superpowers/plans/2026-09-26-local-evaluation-environment-commit-plan.md
  git diff --cached --stat
  git diff --cached --check
  ```

- [ ] Commit:

  ```bash
  git commit -m "docs(local-eval): document local evaluation environment"
  ```

---

### Commit 6: Gap 1 design and progress record

**Commit message:** `docs(evaluation): document production instrumentation gap`

**Files:**

- Create: `docs/evaluation-plane-gap-analysis.md`
- Create: `docs/evaluation_implementation/gap-01-production-run-instrumentation.md`

**Responsibility:** Land the linked Gap 1 analysis and implementation plan together, including the completed PR 0 checklist, verification evidence, and remaining PR 1–5 sequence.

**Scope warning:** Both files were already untracked before the local-environment implementation. Review their complete contents before staging because Git cannot separate earlier untracked content from the progress section added during this work.

- [ ] Confirm the documents link to each other and the local guide:

  ```bash
  test -f docs/evaluation-plane-gap-analysis.md
  test -f docs/evaluation_implementation/local-environment.md
  rg -n "evaluation-plane gap analysis|local-environment.md|PR 0: Local development foundation" docs/evaluation_implementation/gap-01-production-run-instrumentation.md
  ```

- [ ] Review the complete files, not only a diff:

  ```bash
  git diff --no-index -- /dev/null docs/evaluation-plane-gap-analysis.md
  git diff --no-index -- /dev/null docs/evaluation_implementation/gap-01-production-run-instrumentation.md
  ```

  Expected: `git diff --no-index` exits with status 1 because content differs from `/dev/null`; that status is normal. Review the output for scope before proceeding.

- [ ] Stage and inspect only the linked Gap 1 documents:

  ```bash
  git add docs/evaluation-plane-gap-analysis.md docs/evaluation_implementation/gap-01-production-run-instrumentation.md
  git diff --cached --stat
  git diff --cached --check
  ```

- [ ] Commit:

  ```bash
  git commit -m "docs(evaluation): document production instrumentation gap"
  ```

---

## Final verification

- [ ] Run the complete verification set from the repository root:

  ```bash
  make local-check
  make local-smoke
  pytest -q
  python -m compileall -q services/fake_models services/aws_emulator scripts/smoke_local_env.py
  git diff --check
  ```

- [ ] Confirm the commit order and scope:

  ```bash
  git log -6 --oneline
  git status --short
  ```

  Expected commit order, newest first:

  ```text
  docs(evaluation): document production instrumentation gap
  docs(local-eval): document local evaluation environment
  feat(local-eval): add isolated evaluation stack
  feat(observability): add local OTLP collector
  feat(local-eval): add versioned S3 emulator
  feat(local-eval): add deterministic model service
  ```

  Expected remaining untracked files are only the explicitly excluded documentation files.

## Self-review

- Each file belongs to exactly one of the six commits.
- Each executable commit records a red→green cycle but lands only after its focused suite is green.
- Overall acceptance tests and orchestration land together in Commit 4.
- Compose lands after every service and Collector configuration it references.
- No intentionally red commit lands in repository history.
- Documentation lands after behavior, so each earlier commit remains code-reviewable without prose churn.
- The Gap 1 documents are isolated because they contain earlier untracked work that requires whole-file review.
- No production tracing, evidence writing, queueing, cloud deployment, or Opik integration is represented as complete.
