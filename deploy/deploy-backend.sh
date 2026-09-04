#!/usr/bin/env bash
#
# Deploys the backend: uploads the zip to S3, creates/updates the crucible-api
# Lambda (python3.12, DynamoDB persistence), caps concurrency, and exposes a
# public HTTPS Function URL. Idempotent — safe to re-run.
#
# update-function-configuration REPLACES the Lambda's whole environment, not
# merges it — every var the function needs (including Cognito auth, below)
# must be re-supplied on every run, or a routine redeploy will silently wipe
# it. Set CRUCIBLE_COGNITO_USER_POOL_ID and CRUCIBLE_COGNITO_CLIENT_ID (and
# optionally CRUCIBLE_AUTH_REQUIRED / CRUCIBLE_COGNITO_REGION) in your shell
# before running this if the deployment requires authenticated access.
#
# Prereq: deploy/setup-iam.sh has been run, and deploy/build/crucible-api.zip
# exists (run deploy/build-lambda.sh first).
#
# Prints the Function URL on success.
set -euo pipefail

ACCOUNT="557690618983"
REGION="us-west-2"
FN="crucible-api"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/crucible-lambda-role"
BUCKET="crucible-artifacts-${ACCOUNT}"
ZIP="$(cd "$(dirname "$0")/.." && pwd)/deploy/build/crucible-api.zip"
KEY="crucible-api.zip"
CONCURRENCY="${CRUCIBLE_CONCURRENCY:-5}"
CORS_ORIGINS="${CRUCIBLE_CORS_ORIGINS:-*}"

# Build the Lambda env as JSON (robust to empty values and URL characters that
# break the CLI's key=value shorthand). Tavus is optional: set
# CRUCIBLE_TAVUS_API_KEY and CRUCIBLE_TAVUS_REPLICA_ID (and optionally
# _PERSONA_ID) in your OWN shell to turn on the video avatar. Never commit these.
ENV_JSON="{\"Variables\":{\"INTERVIEWAI_PERSISTENCE\":\"dynamodb\",\"INTERVIEWAI_CORS_ORIGINS\":\"${CORS_ORIGINS}\""
if [ -n "${CRUCIBLE_TAVUS_API_KEY:-}" ]; then
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_TAVUS_API_KEY\":\"${CRUCIBLE_TAVUS_API_KEY}\""
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_TAVUS_REPLICA_ID\":\"${CRUCIBLE_TAVUS_REPLICA_ID:-}\""
  if [ -n "${CRUCIBLE_TAVUS_PERSONA_ID:-}" ]; then
    ENV_JSON="${ENV_JSON},\"INTERVIEWAI_TAVUS_PERSONA_ID\":\"${CRUCIBLE_TAVUS_PERSONA_ID}\""
  fi
  echo "==> Tavus avatar: ENABLED (key provided)"
fi

# Cognito auth is optional here the same way Tavus is: set
# CRUCIBLE_COGNITO_USER_POOL_ID and CRUCIBLE_COGNITO_CLIENT_ID in your OWN
# shell (never commit these) to require a valid Cognito ID token on every
# /api call. CRUCIBLE_AUTH_REQUIRED defaults to "true" once a pool is
# provided; CRUCIBLE_COGNITO_REGION defaults to the deploy region. Without a
# pool id, update-function-configuration REPLACES the whole environment, so
# omitting these here would silently disable auth on a live deployment.
if [ -n "${CRUCIBLE_COGNITO_USER_POOL_ID:-}" ] && [ -n "${CRUCIBLE_COGNITO_CLIENT_ID:-}" ]; then
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_AUTH_REQUIRED\":\"${CRUCIBLE_AUTH_REQUIRED:-true}\""
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_COGNITO_REGION\":\"${CRUCIBLE_COGNITO_REGION:-$REGION}\""
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_COGNITO_USER_POOL_ID\":\"${CRUCIBLE_COGNITO_USER_POOL_ID}\""
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_COGNITO_CLIENT_ID\":\"${CRUCIBLE_COGNITO_CLIENT_ID}\""
  echo "==> Cognito auth: ENABLED (user pool + client id provided)"
else
  echo "==> Cognito auth: DISABLED — set CRUCIBLE_COGNITO_USER_POOL_ID and CRUCIBLE_COGNITO_CLIENT_ID to require auth"
fi
ENV_JSON="${ENV_JSON}}}"

[ -f "$ZIP" ] || { echo "Missing $ZIP — run deploy/build-lambda.sh first"; exit 1; }

echo "==> Artifact bucket"
aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || \
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
aws s3 cp "$ZIP" "s3://${BUCKET}/${KEY}" >/dev/null
echo "    uploaded s3://${BUCKET}/${KEY}"

if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  echo "==> Updating existing function code"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --s3-bucket "$BUCKET" --s3-key "$KEY" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
    --environment "$ENV_JSON" >/dev/null
else
  echo "==> Creating function"
  aws lambda create-function --function-name "$FN" --region "$REGION" \
    --runtime python3.12 --handler lambda_handler.handler \
    --role "$ROLE_ARN" --timeout 60 --memory-size 1024 \
    --code "S3Bucket=${BUCKET},S3Key=${KEY}" \
    --environment "$ENV_JSON" >/dev/null
  aws lambda wait function-active --function-name "$FN" --region "$REGION"
fi

echo "==> Capping concurrency at ${CONCURRENCY} (cost ceiling)"
# On accounts with a low total concurrency limit this is rejected (it would
# starve the shared pool) — in that case the account-wide limit is itself the
# cap, so treat the failure as non-fatal.
aws lambda put-function-concurrency --function-name "$FN" --region "$REGION" \
  --reserved-concurrent-executions "$CONCURRENCY" >/dev/null 2>&1 \
  && echo "    reserved ${CONCURRENCY}" \
  || echo "    skipped (account concurrency limit already caps it)"

# Public exposure via API Gateway HTTP API (not a Lambda Function URL — those
# are blocked by an account guardrail on this account). CORS is handled inside
# FastAPI via INTERVIEWAI_CORS_ORIGINS, so the API itself stays CORS-agnostic.
echo "==> API Gateway HTTP API"
API_ARN="arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FN}"
API_ID=$(aws apigatewayv2 get-apis --region "$REGION" \
  --query "Items[?Name=='${FN}'].ApiId | [0]" --output text 2>/dev/null)
if [ -z "$API_ID" ] || [ "$API_ID" = "None" ]; then
  API_ID=$(aws apigatewayv2 create-api --name "$FN" --protocol-type HTTP \
    --target "$API_ARN" --region "$REGION" --query ApiId --output text)
fi

# The quick-create permission is unreliable; ensure API Gateway can invoke.
aws lambda add-permission --function-name "$FN" --region "$REGION" \
  --statement-id apigw-invoke --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT}:${API_ID}/*/*" >/dev/null 2>&1 || true

URL=$(aws apigatewayv2 get-api --api-id "$API_ID" --region "$REGION" --query ApiEndpoint --output text)
echo ""
echo "Backend live: ${URL}"
echo "Health:       ${URL}/api/health"
