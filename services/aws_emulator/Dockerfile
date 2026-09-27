FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY services/aws_emulator/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY services /app/services
RUN chmod +x /app/services/aws_emulator/entrypoint.sh

EXPOSE 4566

CMD ["/app/services/aws_emulator/entrypoint.sh"]
