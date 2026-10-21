# freight-docs-extract: Cloud Run service (Eventarc, GCS object finalized).
# Build context is the repo root, so the 7501 parser is imported from
# tools/customs_entries_load.py as-is:
#   docker build -f services/freight_docs_extract/Dockerfile .
# deploy/deploy.sh stages the same paths and builds with Cloud Build.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/freight_docs_extract/requirements.txt services/freight_docs_extract/
RUN pip install --no-cache-dir -r services/freight_docs_extract/requirements.txt
COPY tools/customs_entries_load.py tools/
COPY services/freight_docs_extract/main.py services/freight_docs_extract/

ENV PYTHONUNBUFFERED=1 FREIGHT_DOCS_TOOLS_DIR=/app/tools
USER nobody
CMD ["python", "services/freight_docs_extract/main.py"]
