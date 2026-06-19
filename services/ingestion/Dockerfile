# CPU-only image shared by the ingestion Ray head/workers and the SQS consumer.
FROM rayproject/ray:2.9.0-py310

USER root
COPY services/ingestion/constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir --constraint /tmp/constraints.txt \
      "setuptools<81" \
      "protobuf==3.20.3" \
      "numpy==1.26.2" \
      "boto3==1.34.0" \
      "httpx==0.26.0" \
      "qdrant-client==1.7.0" \
      "neo4j==5.14.0" \
      "langchain==0.1.5" \
      python-docx python-pptx beautifulsoup4 \
  && pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      "torch==2.0.1+cpu" \
      "torchvision==0.15.2+cpu" \
  && pip install --no-cache-dir \
      --constraint /tmp/constraints.txt \
      "unstructured[pdf]==0.11.0" \
      pypdf pypdfium2 pi-heif \
  && pip uninstall -y opencv-python \
  && pip install --no-cache-dir --force-reinstall --no-deps \
      "opencv-python-headless==4.8.1.78" \
      "numpy==1.26.2" \
      "protobuf==3.20.3"

WORKDIR /app
COPY pipelines /app/pipelines
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1

USER ray
CMD ["python", "-m", "pipelines.jobs.sqs_ingestion_worker"]
