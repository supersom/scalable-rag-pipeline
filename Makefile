# Makefile

LOCAL_COMPOSE = docker compose -p scalable-rag-eval --profile eval-local --env-file .env.example
EVIDENCE_COMPOSE = $(LOCAL_COMPOSE) -f docker-compose.yml -f docker-compose.evaluation.yml
EVIDENCE_WAIT_SECONDS ?= 300
LOAD_TEST_HOST ?= http://127.0.0.1:18000

.PHONY: help install dev ray-serve ray-stop up down local-up local-down local-check local-smoke local-evidence-up local-evidence-down local-evidence-check local-evidence-smoke inspect-traces inspect-evidence load-test-install load-test deploy test infra

help:
	@echo "RAG Platform Commands:"
	@echo "  make install    - Install Python dependencies"
	@echo "  make dev        - Run FastAPI server locally"
	@echo "  make up         - Start local DBs (Docker)"
	@echo "  make down       - Stop local DBs"
	@echo "  make local-up   - Start DBs, fake models, local S3, and OTel Collector"
	@echo "  make local-down - Stop the complete local evaluation stack"
	@echo "  make local-check - Validate the local Compose profile and run its contract tests"
	@echo "  make local-smoke - Exercise the running local stack end to end"
	@echo "  make inspect-traces - Show recent spans from local Collector logs"
	@echo "  make inspect-evidence - Query persisted local evidence (requires evidence stack)"
	@echo "  make local-evidence-up    - Local stack plus evidence migrations, SQS queues and delivery worker"
	@echo "  make local-evidence-down  - Stop the evidence stack (volumes are kept)"
	@echo "  make local-evidence-check - Validate the evidence override and run its contract tests"
	@echo "  make local-evidence-smoke - Check the running evidence stack and the default smoke contract"
	@echo "  make load-test-install - Install the optional Locust dependency"
	@echo "  make load-test  - Open Locust against the local API"
	@echo "  make deploy     - Deploy to AWS EKS via Helm"
	@echo "  make infra      - Apply Terraform"

install:
	pip install -r services/api/requirements.txt
	pip install -r pipelines/ingestion/requirements.txt # (Hypothetical separate deps)

# Run Local Development Environment
up:
	docker-compose up -d

down:
	docker-compose down

local-up:
	$(LOCAL_COMPOSE) up -d --build --wait

local-down:
	$(LOCAL_COMPOSE) down

local-check:
	$(LOCAL_COMPOSE) config --quiet
	pytest tests/local_env -q

local-smoke:
	RUN_LOCAL_STACK_TESTS=1 pytest tests/local_env/test_running_stack.py tests/local_env/test_otel_collector.py -q

inspect-traces:
	./scripts/inspect_traces.sh $(ARGS)

inspect-evidence:
	./scripts/inspect_evidence.sh $(ARGS)

# Same project as local-up: the override recreates api-local with evidence enabled and adds
# the migration job and delivery worker. down never passes -v, so volumes survive.
local-evidence-up:
	$(EVIDENCE_COMPOSE) up -d --build --wait --wait-timeout $(EVIDENCE_WAIT_SECONDS)

local-evidence-down:
	$(EVIDENCE_COMPOSE) down

local-evidence-check:
	$(EVIDENCE_COMPOSE) config --quiet
	pytest tests/local_env/test_eval_queue.py tests/local_env/test_local_configuration.py tests/local_env/test_aws_emulator.py -q

local-evidence-smoke:
	RUN_LOCAL_EVIDENCE_TESTS=1 RUN_LOCAL_STACK_TESTS=1 pytest tests/local_env/test_eval_queue.py tests/local_env/test_running_stack.py -q

load-test-install:
	pip install -r requirements-loadtest.txt

load-test:
	locust -f scripts/load_test.py --host $(LOAD_TEST_HOST)

# Run the API locally (Hot Reload)
# Exclude scripts/ (not imported by FastAPI) and services/models/ (Ray Serve files —
# edited separately; Ray Serve process touching them caused a continuous reload loop)
dev:
	uvicorn services.api.main:app --reload --host 0.0.0.0 --port 8000 --env-file .env \
		--reload-exclude 'scripts/*' --reload-exclude 'services/models/*'

# Serve LLM + Embedding models via Ray Serve (alternative to Ollama)
# Endpoints: http://localhost:8001/v1/chat/completions
#            http://localhost:8001/api/embeddings
# Use PROFILE=cloud for cloud deployment: make ray-serve PROFILE=cloud
ray-serve:
	python -m services.models.serve --profile $(or $(PROFILE),local)

ray-stop:
	ray stop --force

# Infrastructure
infra:
	cd infra/terraform && terraform init && terraform apply

# Kubernetes Deployment
deploy:
	# Update dependencies
	helm dependency update deploy/helm/api
	# Install/Upgrade
	helm upgrade --install api deploy/helm/api --namespace default
	helm upgrade --install ray-cluster kuberay/ray-cluster -f deploy/ray/ray-cluster.yaml

test:
	pytest tests/
