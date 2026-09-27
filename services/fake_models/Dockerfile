FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY services/fake_models/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY services /app/services

EXPOSE 8000

CMD ["uvicorn", "services.fake_models.app:app", "--host", "0.0.0.0", "--port", "8000"]
