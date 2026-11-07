#!/usr/bin/env bash
# deploy.sh — Deploy/update all three Cloud Functions (weekly digest, last-mile
# horizon scanner, feedback form), IAM bindings, and Cloud Scheduler jobs.
#
# Usage:
#   cd weekly-digest
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Safe to re-run: gcloud deploy/create commands here are idempotent or
# explicitly check-before-create where that matters.

set -euo pipefail

PROJECT_ID="project-5ce9338e-bc20-4b9f-88f"
REGION="us-west1"
SA_EMAIL="project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com"
SA_FULL="projects/${PROJECT_ID}/serviceAccounts/${SA_EMAIL}"

echo "== Setting active project =="
gcloud config set project "${PROJECT_ID}"

echo "== Enabling required APIs =="
gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  cloudscheduler.googleapis.com \
  compute.googleapis.com

echo "== Ensuring Firestore database exists =="
if ! gcloud firestore databases describe --database="(default)" >/dev/null 2>&1; then
  gcloud firestore databases create --location="${REGION}" --type=firestore-native
else
  echo "Firestore database already exists, skipping."
fi

echo "== Ensuring Firestore composite index for feedback query =="
set +e
INDEX_OUTPUT=$(gcloud firestore indexes composite create \
  --collection-group=report_feedback \
  --field-config field-path=report_type,order=ascending \
  --field-config field-path=addressed,order=ascending \
  --field-config field-path=submitted_at,order=descending 2>&1)
INDEX_EXIT=$?
set -e
if [ ${INDEX_EXIT} -ne 0 ]; then
  if echo "${INDEX_OUTPUT}" | grep -q "ALREADY_EXISTS"; then
    echo "Composite index already exists, skipping."
  else
    echo "${INDEX_OUTPUT}"
    echo "Onverwachte fout bij aanmaken index — script stopt."
    exit 1
  fi
else
  echo "${INDEX_OUTPUT}"
fi

echo "== IAM: Secret Manager access =="
for secret in gmail-client-id gmail-client-secret gmail-refresh-token; do
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
done

echo "== IAM: Vertex AI + Firestore access =="
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/aiplatform.user" >/dev/null

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/datastore.user" >/dev/null

echo "== IAM: build service account permissions =="
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/cloudbuild.builds.builder" >/dev/null

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectViewer" >/dev/null

DEPLOY_COMMON=(
  --gen2
  --runtime=python312
  --region="${REGION}"
  --source=.
  --trigger-http
  --timeout=900
  --memory=512MB
  --service-account="${SA_EMAIL}"
  --build-service-account="${SA_FULL}"
)

echo "== Deploying weekly-regulatory-digest =="
gcloud functions deploy weekly-regulatory-digest \
  "${DEPLOY_COMMON[@]}" \
  --entry-point=weekly_digest \
  --no-allow-unauthenticated

echo "== Deploying last-mile-horizon-scan =="
gcloud functions deploy last-mile-horizon-scan \
  "${DEPLOY_COMMON[@]}" \
  --entry-point=last_mile_digest \
  --no-allow-unauthenticated

echo "== Deploying report-feedback (public, no auth — required for email links) =="
gcloud functions deploy report-feedback \
  --gen2 \
  --runtime=python312 \
  --region="${REGION}" \
  --source=. \
  --trigger-http \
  --timeout=60 \
  --memory=256MB \
  --service-account="${SA_EMAIL}" \
  --build-service-account="${SA_FULL}" \
  --entry-point=report_feedback \
  --allow-unauthenticated

echo "== Deploying vendor-status-update (public, no auth — required for email links) =="
gcloud functions deploy vendor-status-update \
  --gen2 \
  --runtime=python312 \
  --region="${REGION}" \
  --source=. \
  --trigger-http \
  --timeout=60 \
  --memory=256MB \
  --service-account="${SA_EMAIL}" \
  --build-service-account="${SA_FULL}" \
  --entry-point=vendor_status_update \
  --allow-unauthenticated

echo "== Granting Cloud Run invoker to service account for the two scheduled functions =="
gcloud run services add-iam-policy-binding weekly-regulatory-digest \
  --region="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" >/dev/null

gcloud run services add-iam-policy-binding last-mile-horizon-scan \
  --region="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" >/dev/null

WEEKLY_URL=$(gcloud functions describe weekly-regulatory-digest --region="${REGION}" --gen2 --format="value(serviceConfig.uri)")
LAST_MILE_URL=$(gcloud functions describe last-mile-horizon-scan --region="${REGION}" --gen2 --format="value(serviceConfig.uri)")
FEEDBACK_URL=$(gcloud functions describe report-feedback --region="${REGION}" --gen2 --format="value(serviceConfig.uri)")
VENDOR_STATUS_URL=$(gcloud functions describe vendor-status-update --region="${REGION}" --gen2 --format="value(serviceConfig.uri)")

echo "== Creating/updating Cloud Scheduler jobs =="

if gcloud scheduler jobs describe regulatory-digest-weekly --location="${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http regulatory-digest-weekly \
    --location="${REGION}" \
    --schedule="0 8 * * 1" \
    --uri="${WEEKLY_URL}" \
    --oidc-service-account-email="${SA_EMAIL}" \
    --time-zone="Europe/Amsterdam" \
    --attempt-deadline=1800s
else
  gcloud scheduler jobs create http regulatory-digest-weekly \
    --schedule="0 8 * * 1" \
    --uri="${WEEKLY_URL}" \
    --http-method=POST \
    --oidc-service-account-email="${SA_EMAIL}" \
    --location="${REGION}" \
    --time-zone="Europe/Amsterdam" \
    --attempt-deadline=1800s
fi

if gcloud scheduler jobs describe last-mile-horizon-scan-biweekly --location="${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http last-mile-horizon-scan-biweekly \
    --location="${REGION}" \
    --schedule="0 7 * * 1,4" \
    --uri="${LAST_MILE_URL}" \
    --oidc-service-account-email="${SA_EMAIL}" \
    --time-zone="Europe/Amsterdam" \
    --attempt-deadline=1800s
else
  gcloud scheduler jobs create http last-mile-horizon-scan-biweekly \
    --schedule="0 7 * * 1,4" \
    --uri="${LAST_MILE_URL}" \
    --http-method=POST \
    --oidc-service-account-email="${SA_EMAIL}" \
    --location="${REGION}" \
    --time-zone="Europe/Amsterdam" \
    --attempt-deadline=1800s
fi

echo ""
echo "======================================================================"
echo "Deploy klaar."
echo "  weekly-regulatory-digest   : ${WEEKLY_URL}"
echo "  last-mile-horizon-scan     : ${LAST_MILE_URL}"
echo "  report-feedback (public)   : ${FEEDBACK_URL}"
echo "  vendor-status-update (public): ${VENDOR_STATUS_URL}"
echo ""
echo "Test handmatig met:"
echo "  curl -X POST -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" ${WEEKLY_URL}"
echo "  curl -X POST -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" ${LAST_MILE_URL}"
echo "  curl \"${FEEDBACK_URL}?report=weekly\""
echo "======================================================================"
