FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    HOME=/tmp

WORKDIR /app

COPY ui/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

COPY --chown=10001:10001 ui/streamlit_app.py /app/streamlit_app.py

USER 10001
EXPOSE 8501

CMD ["streamlit", "run", "/app/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
