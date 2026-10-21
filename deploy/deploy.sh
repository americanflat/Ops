#!/usr/bin/env bash
# Provision the freight docs PDF ingestion pipeline in one pass.
#
#   Gmail (Apps Script) -> gs://$BUCKET/raw/ -> Eventarc -> freight-docs-extract (Cloud Run)
#   -> gs://$BUCKET/processed/ -> Cloud Scheduler -> freight-docs-load (Cloud Run job) -> BigQuery
#
# Idempotent: every step creates what is missing or updates what exists, and
# nothing else. There is no step that removes a resource or an IAM binding.
# Re-run it to roll out a new image.
#
# Usage:
#   deploy/deploy.sh --dry-run   # print every command, run none (no cloud access needed)
#   deploy/deploy.sh             # do it
#
# Settings: defaults below, overridden by deploy/freight_docs.env if present
# (template: deploy/freight_docs.env.example), overridden by the environment.
#
# Who runs it: a project Owner of americanflat who is also OWNER of the
# `finance` dataset (the last step grants the load service account WRITER on
# it). Tables are NOT created here: see docs/freight-docs-pdf-ingestion.md.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ENV_FILE="${FREIGHT_DOCS_ENV_FILE:-$HERE/freight_docs.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
fi

: "${FREIGHT_DOCS_PROJECT:=americanflat}"
: "${FREIGHT_DOCS_REGION:=us-central1}"
: "${FREIGHT_DOCS_BUCKET:=americanflat-freight-docs}"
: "${FREIGHT_DOCS_DATASET:=finance}"
: "${FREIGHT_DOCS_RAW_TABLE:=freight_documents_raw}"
: "${FREIGHT_DOCS_AR_REPO:=freight-docs}"
: "${FREIGHT_DOCS_EXTRACT_SERVICE:=freight-docs-extract}"
: "${FREIGHT_DOCS_LOAD_JOB:=freight-docs-load}"
: "${FREIGHT_DOCS_TRIGGER:=freight-docs-raw-finalized}"
: "${FREIGHT_DOCS_SCHEDULER_JOB:=freight-docs-load-hourly}"
: "${FREIGHT_DOCS_LOAD_SCHEDULE:=30 * * * *}"
: "${FREIGHT_DOCS_LOAD_TIMEZONE:=Etc/UTC}"
: "${FREIGHT_DOCS_EXTRACT_SA:=freight-docs-extract}"
: "${FREIGHT_DOCS_LOAD_SA:=freight-docs-load}"
: "${FREIGHT_DOCS_TRIGGER_SA:=freight-docs-trigger}"
: "${FREIGHT_DOCS_SCHEDULER_SA:=freight-docs-scheduler}"
: "${FREIGHT_DOCS_EXPORTER:=user:administrator@americanflat.com}"
: "${FREIGHT_DOCS_DOCAI_ENABLED:=1}"
: "${FREIGHT_DOCS_DOCAI_LOCATION:=us}"
: "${FREIGHT_DOCS_DOCAI_PROCESSOR_NAME:=freight-docs-form-parser}"

P="$FREIGHT_DOCS_PROJECT"
R="$FREIGHT_DOCS_REGION"
B="$FREIGHT_DOCS_BUCKET"
sa_email() { echo "$1@$P.iam.gserviceaccount.com"; }
EXTRACT_SA="$(sa_email "$FREIGHT_DOCS_EXTRACT_SA")"
LOAD_SA="$(sa_email "$FREIGHT_DOCS_LOAD_SA")"
TRIGGER_SA="$(sa_email "$FREIGHT_DOCS_TRIGGER_SA")"
SCHEDULER_SA="$(sa_email "$FREIGHT_DOCS_SCHEDULER_SA")"
REGISTRY="$R-docker.pkg.dev/$P/$FREIGHT_DOCS_AR_REPO"

VERSION="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo manual)"
if [[ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]]; then
  VERSION="$VERSION-dirty"
fi
EXTRACT_IMAGE="$REGISTRY/$FREIGHT_DOCS_EXTRACT_SERVICE:$VERSION"
LOAD_IMAGE="$REGISTRY/$FREIGHT_DOCS_LOAD_JOB:$VERSION"

# ---------------------------------------------------------------- helpers

step() { printf '\n==> %s\n' "$*"; }

# Echo the command; run it unless --dry-run.
run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  if (( DRY_RUN )); then return 0; fi
  "$@"
}

# True if the read-only probe succeeds. Under --dry-run nothing is probed and
# the create path is shown.
exists() {
  if (( DRY_RUN )); then
    printf '  (dry run: not checked: %s)\n' "$*"
    return 1
  fi
  "$@" >/dev/null 2>&1
}

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not found on PATH" >&2; exit 4; }
}

# ---------------------------------------------------------------- preflight

step "Preflight"
echo "project=$P region=$R bucket=gs://$B version=$VERSION dry_run=$DRY_RUN"
if (( ! DRY_RUN )); then
  need gcloud; need bq; need curl; need python3
  echo "deploying as: $(gcloud config get-value account 2>/dev/null)"
fi

# ---------------------------------------------------------------- 1. APIs

step "1. Enable APIs"
APIS=(run.googleapis.com eventarc.googleapis.com pubsub.googleapis.com storage.googleapis.com
      artifactregistry.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com
      bigquery.googleapis.com iam.googleapis.com)
if (( FREIGHT_DOCS_DOCAI_ENABLED )); then APIS+=(documentai.googleapis.com); fi
run gcloud services enable "${APIS[@]}" --project="$P"

# ---------------------------------------------------------------- 2. service accounts

step "2. Runtime service accounts"
for pair in \
  "$FREIGHT_DOCS_EXTRACT_SA|freight docs: extract service (Cloud Run)" \
  "$FREIGHT_DOCS_LOAD_SA|freight docs: BigQuery load job (Cloud Run job)" \
  "$FREIGHT_DOCS_TRIGGER_SA|freight docs: Eventarc trigger identity" \
  "$FREIGHT_DOCS_SCHEDULER_SA|freight docs: Cloud Scheduler identity"; do
  id="${pair%%|*}"; name="${pair#*|}"
  if exists gcloud iam service-accounts describe "$(sa_email "$id")" --project="$P"; then
    echo "  exists: $(sa_email "$id")"
  else
    run gcloud iam service-accounts create "$id" --display-name="$name" --project="$P"
  fi
done

# ---------------------------------------------------------------- 3. bucket

step "3. Bucket gs://$B"
if exists gcloud storage buckets describe "gs://$B" --project="$P"; then
  echo "  exists: gs://$B"
else
  run gcloud storage buckets create "gs://$B" --project="$P" --location="$R" \
    --uniform-bucket-level-access --public-access-prevention
fi

step "3b. Bucket IAM (additive)"
# objectCreator cannot overwrite or remove objects: together with
# ifGenerationMatch=0 on every write, nothing in this pipeline can replace
# an object once written.
bucket_bind() { run gcloud storage buckets add-iam-policy-binding "gs://$B" --member="$1" --role="$2" --project="$P"; }
bucket_bind "$FREIGHT_DOCS_EXPORTER"         roles/storage.objectCreator
bucket_bind "serviceAccount:$EXTRACT_SA"     roles/storage.objectViewer
bucket_bind "serviceAccount:$EXTRACT_SA"     roles/storage.objectCreator
bucket_bind "serviceAccount:$LOAD_SA"        roles/storage.objectViewer
bucket_bind "serviceAccount:$LOAD_SA"        roles/storage.objectCreator

# ---------------------------------------------------------------- 4. project IAM

step "4. Project IAM (additive)"
if (( DRY_RUN )); then
  PROJECT_NUMBER="PROJECT_NUMBER"
  GCS_AGENT="service-PROJECT_NUMBER@gs-project-accounts.iam.gserviceaccount.com"
else
  PROJECT_NUMBER="$(gcloud projects describe "$P" --format='value(projectNumber)')"
  GCS_AGENT="$(gcloud storage service-agent --project="$P")"
fi
project_bind() {
  run gcloud projects add-iam-policy-binding "$P" --member="$1" --role="$2" --condition=None --quiet
}
project_bind "serviceAccount:$TRIGGER_SA" roles/eventarc.eventReceiver
project_bind "serviceAccount:$GCS_AGENT"  roles/pubsub.publisher
project_bind "serviceAccount:service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com" \
  roles/iam.serviceAccountTokenCreator
project_bind "serviceAccount:$LOAD_SA"    roles/bigquery.jobUser
if (( FREIGHT_DOCS_DOCAI_ENABLED )); then
  project_bind "serviceAccount:$EXTRACT_SA" roles/documentai.apiUser
fi

# ---------------------------------------------------------------- 5. Artifact Registry

step "5. Artifact Registry $REGISTRY"
if exists gcloud artifacts repositories describe "$FREIGHT_DOCS_AR_REPO" --location="$R" --project="$P"; then
  echo "  exists: $FREIGHT_DOCS_AR_REPO"
else
  run gcloud artifacts repositories create "$FREIGHT_DOCS_AR_REPO" --repository-format=docker \
    --location="$R" --description="freight docs PDF ingestion images" --project="$P"
fi

# ---------------------------------------------------------------- 6. images

# Each image needs tools/customs_entries_load.py from outside its service
# directory, so build from a staging copy holding only the files the
# Dockerfile copies (the paths mirror the repo, with the Dockerfile at the
# root). That also keeps anything else in the working tree out of the upload.
build_image() {
  local svc="$1" image="$2"; shift 2
  local ctx
  if (( DRY_RUN )); then ctx="<staging-dir-for-$svc>"; else ctx="$(mktemp -d)"; fi
  run cp "$ROOT/services/$svc/Dockerfile" "$ctx/Dockerfile"
  local f
  for f in "$@"; do
    run mkdir -p "$ctx/$(dirname "$f")"
    run cp "$ROOT/$f" "$ctx/$f"
  done
  run gcloud builds submit "$ctx" --tag="$image" --region="$R" --project="$P"
}

step "6. Build images ($VERSION)"
build_image freight_docs_extract "$EXTRACT_IMAGE" \
  services/freight_docs_extract/main.py services/freight_docs_extract/requirements.txt \
  tools/customs_entries_load.py
build_image freight_docs_load "$LOAD_IMAGE" \
  services/freight_docs_load/main.py services/freight_docs_load/requirements.txt \
  tools/customs_entries_load.py schemas/freight_documents_raw.json

# ---------------------------------------------------------------- 7. Document AI

DOCAI_PROCESSOR_ID=""
if (( FREIGHT_DOCS_DOCAI_ENABLED )); then
  step "7. Document AI Form Parser processor ($FREIGHT_DOCS_DOCAI_PROCESSOR_NAME, $FREIGHT_DOCS_DOCAI_LOCATION)"
  # No GA gcloud command manages processors; use the REST API with the
  # deployer's token. Look up by display name first so a re-run reuses it.
  DOCAI_API="https://$FREIGHT_DOCS_DOCAI_LOCATION-documentai.googleapis.com/v1/projects/$P/locations/$FREIGHT_DOCS_DOCAI_LOCATION/processors"
  find_processor() {
    curl -fsS -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "x-goog-user-project: $P" \
      "$DOCAI_API" | python3 -c '
import json, sys
name = sys.argv[1]
for p in json.load(sys.stdin).get("processors", []):
    if p.get("displayName") == name:
        print(p["name"].rsplit("/", 1)[1]); break
' "$FREIGHT_DOCS_DOCAI_PROCESSOR_NAME"
  }
  if (( DRY_RUN )); then
    printf '+ curl GET %s  # find displayName=%s\n' "$DOCAI_API" "$FREIGHT_DOCS_DOCAI_PROCESSOR_NAME"
    printf '+ curl POST %s  {"type":"FORM_PARSER_PROCESSOR","displayName":"%s"}  # only if not found\n' \
      "$DOCAI_API" "$FREIGHT_DOCS_DOCAI_PROCESSOR_NAME"
    DOCAI_PROCESSOR_ID="DOCAI_PROCESSOR_ID"
  else
    DOCAI_PROCESSOR_ID="$(find_processor)"
    if [[ -z "$DOCAI_PROCESSOR_ID" ]]; then
      echo "+ curl POST $DOCAI_API (create FORM_PARSER_PROCESSOR $FREIGHT_DOCS_DOCAI_PROCESSOR_NAME)"
      curl -fsS -X POST -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        -H "x-goog-user-project: $P" -H "Content-Type: application/json" \
        -d "{\"type\":\"FORM_PARSER_PROCESSOR\",\"displayName\":\"$FREIGHT_DOCS_DOCAI_PROCESSOR_NAME\"}" \
        "$DOCAI_API" >/dev/null
      DOCAI_PROCESSOR_ID="$(find_processor)"
    fi
    [[ -n "$DOCAI_PROCESSOR_ID" ]] || { echo "ERROR: Document AI processor not found after create" >&2; exit 5; }
    echo "  processor id: $DOCAI_PROCESSOR_ID"
  fi
else
  step "7. Document AI: disabled (FREIGHT_DOCS_DOCAI_ENABLED=0); extract runs pdftotext only"
fi

# ---------------------------------------------------------------- 8. extract service

COMMON_ENV="FREIGHT_DOCS_PROJECT=$P,FREIGHT_DOCS_BUCKET=$B,FREIGHT_DOCS_VERSION=$VERSION"

step "8. Cloud Run service $FREIGHT_DOCS_EXTRACT_SERVICE"
run gcloud run deploy "$FREIGHT_DOCS_EXTRACT_SERVICE" --image="$EXTRACT_IMAGE" --region="$R" --project="$P" \
  --service-account="$EXTRACT_SA" --no-allow-unauthenticated --ingress=all \
  --cpu=1 --memory=1Gi --concurrency=4 --timeout=540 --min-instances=0 --max-instances=3 \
  --set-env-vars="$COMMON_ENV,FREIGHT_DOCS_DOCAI_LOCATION=$FREIGHT_DOCS_DOCAI_LOCATION,FREIGHT_DOCS_DOCAI_PROCESSOR_ID=$DOCAI_PROCESSOR_ID"
run gcloud run services add-iam-policy-binding "$FREIGHT_DOCS_EXTRACT_SERVICE" --region="$R" --project="$P" \
  --member="serviceAccount:$TRIGGER_SA" --role=roles/run.invoker

# ---------------------------------------------------------------- 9. Eventarc

step "9. Eventarc trigger $FREIGHT_DOCS_TRIGGER (GCS finalize on gs://$B -> $FREIGHT_DOCS_EXTRACT_SERVICE)"
if exists gcloud eventarc triggers describe "$FREIGHT_DOCS_TRIGGER" --location="$R" --project="$P"; then
  run gcloud eventarc triggers update "$FREIGHT_DOCS_TRIGGER" --location="$R" --project="$P" \
    --destination-run-service="$FREIGHT_DOCS_EXTRACT_SERVICE" --destination-run-region="$R" \
    --service-account="$TRIGGER_SA"
else
  run gcloud eventarc triggers create "$FREIGHT_DOCS_TRIGGER" --location="$R" --project="$P" \
    --destination-run-service="$FREIGHT_DOCS_EXTRACT_SERVICE" --destination-run-region="$R" \
    --event-filters="type=google.cloud.storage.object.v1.finalized" \
    --event-filters="bucket=$B" \
    --service-account="$TRIGGER_SA"
fi

# ---------------------------------------------------------------- 10. load job

step "10. Cloud Run job $FREIGHT_DOCS_LOAD_JOB"
JOB_FLAGS=(--image="$LOAD_IMAGE" --region="$R" --project="$P" --service-account="$LOAD_SA"
           --tasks=1 --max-retries=0 --task-timeout=1800 --cpu=1 --memory=1Gi
           --set-env-vars="$COMMON_ENV,FREIGHT_DOCS_DATASET=$FREIGHT_DOCS_DATASET,FREIGHT_DOCS_RAW_TABLE=$FREIGHT_DOCS_RAW_TABLE")
if exists gcloud run jobs describe "$FREIGHT_DOCS_LOAD_JOB" --region="$R" --project="$P"; then
  run gcloud run jobs update "$FREIGHT_DOCS_LOAD_JOB" "${JOB_FLAGS[@]}"
else
  run gcloud run jobs create "$FREIGHT_DOCS_LOAD_JOB" "${JOB_FLAGS[@]}"
fi
run gcloud run jobs add-iam-policy-binding "$FREIGHT_DOCS_LOAD_JOB" --region="$R" --project="$P" \
  --member="serviceAccount:$SCHEDULER_SA" --role=roles/run.invoker

# ---------------------------------------------------------------- 11. scheduler

step "11. Cloud Scheduler $FREIGHT_DOCS_SCHEDULER_JOB ($FREIGHT_DOCS_LOAD_SCHEDULE $FREIGHT_DOCS_LOAD_TIMEZONE)"
RUN_URI="https://run.googleapis.com/v2/projects/$P/locations/$R/jobs/$FREIGHT_DOCS_LOAD_JOB:run"
SCHED_FLAGS=(--location="$R" --project="$P" --schedule="$FREIGHT_DOCS_LOAD_SCHEDULE"
             --time-zone="$FREIGHT_DOCS_LOAD_TIMEZONE" --uri="$RUN_URI" --http-method=POST
             --oauth-service-account-email="$SCHEDULER_SA")
if exists gcloud scheduler jobs describe "$FREIGHT_DOCS_SCHEDULER_JOB" --location="$R" --project="$P"; then
  run gcloud scheduler jobs update http "$FREIGHT_DOCS_SCHEDULER_JOB" "${SCHED_FLAGS[@]}"
else
  run gcloud scheduler jobs create http "$FREIGHT_DOCS_SCHEDULER_JOB" "${SCHED_FLAGS[@]}"
fi

# ---------------------------------------------------------------- 12. dataset grant

step "12. BigQuery: $LOAD_SA gets WRITER (roles/bigquery.dataEditor) on $P.$FREIGHT_DOCS_DATASET"
# GRANT is additive and a no-op when the binding exists. Needs the dataset
# OWNER. It does not touch invoice-writer@ or any other existing grant.
run bq query --project_id="$P" --location=US --use_legacy_sql=false \
  "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`$P.$FREIGHT_DOCS_DATASET\` TO \"serviceAccount:$LOAD_SA\""

# ---------------------------------------------------------------- done

step "Done (version $VERSION)"
cat <<EOF
Not done by this script (see docs/freight-docs-pdf-ingestion.md, "Blockers before first run"):
  * create $P.$FREIGHT_DOCS_DATASET.$FREIGHT_DOCS_RAW_TABLE from schemas/freight_documents_raw.sql
  * create $P.$FREIGHT_DOCS_DATASET.customs_entries: python3 tools/customs_entries_load.py --init
  * install the Apps Script in the Administrator@ account and run installHourlyTrigger
Run the load once by hand:
  gcloud run jobs execute $FREIGHT_DOCS_LOAD_JOB --region=$R --project=$P --wait
EOF
