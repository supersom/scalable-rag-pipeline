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
