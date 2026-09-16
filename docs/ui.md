# Streamlit Chat UI

This UI replaces the manual curl workflow for the authenticated streaming chat
endpoint.

## Why Streamlit

Streamlit is a better fit than Gradio for this repository because the UI is an
operator-facing client rather than a model demo. It gives straightforward
session state, sidebar controls for API/JWT settings, chat rendering, and trace
panels for the NDJSON events emitted by `/api/v1/chat/stream`.

## Run

Install the UI dependencies:

```bash
pip install -r ui/requirements.txt
```

Start the FastAPI service separately:

```bash
make dev
```

Start the UI:

```bash
streamlit run ui/streamlit_app.py
```

The app defaults to `http://localhost:8000` and signs a development JWT using
`JWT_SECRET_KEY` from the environment, falling back to the same development
secret used by `scripts/eval_pipeline.py`.

Use **New session** to create a fresh chat `session_id` and token. Use
**Refresh JWT** to renew the token without clearing the visible chat history.

## Deploy to EKS

The cluster deployment uses managed-auth mode: the API URL and JWT signing
secret remain server-side and are not rendered as sidebar controls. The service
is intentionally internal until an authenticated ingress is installed.

```bash
export UI_IMAGE_TAG=$(git rev-parse --short HEAD)
UI_IMAGE="$(terraform -chdir=infra/terraform output -raw ui_ecr_repository_url):${UI_IMAGE_TAG}"

aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin \
    764988411032.dkr.ecr.us-east-1.amazonaws.com

docker buildx build --push \
  --tag "${UI_IMAGE}" \
  --file ui/Dockerfile .

UI_IMAGE_TAG="${UI_IMAGE_TAG}" ./scripts/deploy_ui.sh
```

For temporary operator access:

```bash
kubectl port-forward service/ui-service 8501:80
```

Then open `http://localhost:8501`.
