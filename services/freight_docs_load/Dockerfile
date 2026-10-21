# freight-docs-load: Cloud Run job (Cloud Scheduler, hourly).
# Needs gcloud + bq (the 7501 loader shells out to them, as do the landing
# loads) and pdftotext (the 7501 loader reads the PDF itself), so it starts
# from the Cloud SDK image. Build context is the repo root:
#   docker build -f services/freight_docs_load/Dockerfile .
FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# requirements.txt lists no packages (stdlib only), and this image has no pip,
# so there is no install step; add one here if that ever changes.
COPY tools/customs_entries_load.py tools/
COPY schemas/freight_documents_raw.json schemas/
COPY services/freight_docs_load/main.py services/freight_docs_load/

# gcloud/bq keep their state under the config dir; make it writable for nobody.
ENV PYTHONUNBUFFERED=1 FREIGHT_DOCS_TOOLS_DIR=/app/tools HOME=/tmp CLOUDSDK_CONFIG=/tmp/gcloud
USER nobody
ENTRYPOINT ["python3", "services/freight_docs_load/main.py"]
