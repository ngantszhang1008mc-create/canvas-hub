#!/usr/bin/env bash
# Deploy Canvas Hub Public Sandbox Demo to Google Cloud Run (Hong Kong: asia-east2)
# with native Vertex AI (Gemini) via Application Default Credentials (no API key needed).
#
# Usage:
#   bash scripts/deploy-cloudrun-demo.sh <GCP_PROJECT_ID> [GEMINI_MODEL]
#
# Example:
#   bash scripts/deploy-cloudrun-demo.sh my-aisc-project gemini-3.8-flash
set -euo pipefail

PROJECT_ID="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
MODEL="${2:-gemini-3.8-flash}"
REGION="${CLOUDRUN_REGION:-asia-east2}"
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
SERVICE_NAME="${CLOUDRUN_SERVICE:-canvas-hub-demo}"
SA_NAME="canvas-hub-demo-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_NAME="canvas-hub-demo"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${SERVICE_NAME}:latest"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Usage: bash scripts/deploy-cloudrun-demo.sh <GCP_PROJECT_ID> [GEMINI_MODEL]" >&2
  exit 1
fi

echo "==> Target project: ${PROJECT_ID} | Region: ${REGION} | Vertex model: ${MODEL} (${VERTEX_LOCATION})"

echo "==> 1/5 Enabling required GCP APIs (Cloud Run, Cloud Build, Artifact Registry, Vertex AI)..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  --project="${PROJECT_ID}"

echo "==> 2/5 Ensuring least-privilege Service Account (${SA_EMAIL}) with roles/aiplatform.user..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="Canvas Hub Public Demo (Vertex AI User)" \
    --project="${PROJECT_ID}"
fi
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/aiplatform.user" \
  --condition=None >/dev/null

echo "==> 3/5 Ensuring Artifact Registry repository (${REPO_NAME})..."
if ! gcloud artifacts repositories describe "${REPO_NAME}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPO_NAME}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Canvas Hub Public Sandbox Demo images" \
    --project="${PROJECT_ID}"
fi

echo "==> 4/5 Building container image via Cloud Build (Dockerfile.demo)..."
CLOUDBUILD_TMP="$(mktemp /tmp/cloudbuild-canvas-hub-XXXXXX.yaml)"
trap 'rm -f "${CLOUDBUILD_TMP}"' EXIT
cat > "${CLOUDBUILD_TMP}" <<EOF
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "Dockerfile.demo", "-t", "${IMAGE}", "."]
images:
  - "${IMAGE}"
EOF
gcloud builds submit --config="${CLOUDBUILD_TMP}" --project="${PROJECT_ID}" .

echo "==> 5/5 Deploying to Cloud Run (min=1, max=1 for zero cold-start & consistent SQLite state)..."
gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --port=8730 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=1 \
  --max-instances=1 \
  --concurrency=80 \
  --service-account="${SA_EMAIL}" \
  --allow-unauthenticated \
  --set-env-vars="CANVAS_HUB_DEMO_VERTEX_PROJECT=${PROJECT_ID},CANVAS_HUB_DEMO_VERTEX_LOCATION=${VERTEX_LOCATION},CANVAS_HUB_DEMO_VERTEX_MODEL=${MODEL},CANVAS_HUB_DEMO_MAX_OUTPUT_TOKENS=2048,CANVAS_HUB_DEMO_AI_RPM_PER_IP=6,CANVAS_HUB_DEMO_AI_MAX_PER_HOUR=120,CANVAS_HUB_DEMO_AI_MAX_PER_DAY=600"

URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
echo ""
echo "==================================================================="
echo "Canvas Hub Public Sandbox Demo is LIVE!"
echo "  Demo URL:   ${URL}"
echo "  Healthz:    ${URL}/healthz"
echo "  AI Backend: Vertex AI (${MODEL} @ ${VERTEX_LOCATION}) via ADC"
echo "  Rate Limit: 6 req/min/IP · 120 req/hour · 600 req/day"
echo ""
echo "After AISC1000A grading closes (Dec 10+), tear down with:"
echo "  gcloud run services delete ${SERVICE_NAME} --project=${PROJECT_ID} --region=${REGION}"
echo "==================================================================="
