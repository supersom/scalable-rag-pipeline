# CPU-only image shared by the ingestion Ray head/workers and the SQS consumer.
FROM rayproject/ray:2.9.0-py310

USER root
RUN pip install --no-cache-dir \
      "setuptools<81" \
      "protobuf==3.19.6" \
      "numpy==1.26.2" \
      "boto3==1.34.0" \
      "httpx==0.26.0" \
      "qdrant-client==1.7.0" \
      "neo4j==5.14.0" \
      "langchain==0.1.5" \
      "unstructured==0.11.0" \
      pdfminer.six pdf2image pypdf pypdfium2 pi-heif \
      python-docx python-pptx beautifulsoup4

WORKDIR /app
COPY pipelines /app/pipelines
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1

USER ray
CMD ["python", "-m", "pipelines.jobs.sqs_ingestion_worker"]
