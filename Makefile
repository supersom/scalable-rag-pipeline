# Makefile

.PHONY: help install dev ray-serve ray-stop up down deploy test infra

help:
	@echo "RAG Platform Commands:"
	@echo "  make install    - Install Python dependencies"
	@echo "  make dev        - Run FastAPI server locally"
	@echo "  make up         - Start local DBs (Docker)"
	@echo "  make down       - Stop local DBs"
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