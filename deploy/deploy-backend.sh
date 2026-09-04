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

# Cognito auth is UNLIKE Tavus: it's a live access-control boundary, not a
# cosmetic feature, and update-function-configuration REPLACES the whole
# Lambda environment on every run. Silently omitting these vars would
# silently revert a deployment that currently enforces auth back to
# anonymous access — no error, no warning. So this is hard-fail-by-default,
# not warn-and-continue: set CRUCIBLE_COGNITO_USER_POOL_ID and
# CRUCIBLE_COGNITO_CLIENT_ID in your OWN shell (never commit these) to keep
# auth on. CRUCIBLE_AUTH_REQUIRED defaults to "true" once a pool is
# provided; CRUCIBLE_COGNITO_REGION defaults to the deploy region. To
# deploy WITHOUT auth on purpose, set CRUCIBLE_COGNITO_CONFIRM_DISABLE=1.
AUTH_ENABLED=0
if [ -n "${CRUCIBLE_COGNITO_USER_POOL_ID:-}" ] && [ -n "${CRUCIBLE_COGNITO_CLIENT_ID:-}" ]; then
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_AUTH_REQUIRED\":\"${CRUCIBLE_AUTH_REQUIRED:-true}\""
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_COGNITO_REGION\":\"${CRUCIBLE_COGNITO_REGION:-$REGION}\""
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_COGNITO_USER_POOL_ID\":\"${CRUCIBLE_COGNITO_USER_POOL_ID}\""
  ENV_JSON="${ENV_JSON},\"INTERVIEWAI_COGNITO_CLIENT_ID\":\"${CRUCIBLE_COGNITO_CLIENT_ID}\""
  echo "==> Cognito auth: ENABLED (user pool + client id provided)"
  AUTH_ENABLED=1
elif [ "${CRUCIBLE_COGNITO_CONFIRM_DISABLE:-}" = "1" ]; then
  echo ""
  echo "############################################################"
  echo "#  WARNING: DEPLOYING WITH COGNITO AUTH DISABLED            #"
  echo "#  CRUCIBLE_COGNITO_CONFIRM_DISABLE=1 is set. This deploy   #"
  echo "#  will REPLACE the Lambda's environment with auth OFF.     #"
  echo "#  Any client will be able to pass an arbitrary candidate_id#"
  echo "#  and read/write someone else's data. Proceed only if you  #"
  echo "#  intend that for this deployment.                         #"
  echo "############################################################"
  echo ""
else
  {
    echo ""
    echo "ERROR: CRUCIBLE_COGNITO_USER_POOL_ID and CRUCIBLE_COGNITO_CLIENT_ID are"
    echo "not both set."
    echo ""
    echo "update-function-configuration REPLACES the Lambda's entire environment"
    echo "on every deploy. Running this now would SILENTLY DISABLE auth"
    echo "enforcement on a deployment that currently has it turned on — any"
    echo "client could then pass an arbitrary candidate_id and read/write"
    echo "someone else's data."
    echo ""
    echo "Fix: export CRUCIBLE_COGNITO_USER_POOL_ID and CRUCIBLE_COGNITO_CLIENT_ID"
    echo "in this shell (and optionally CRUCIBLE_AUTH_REQUIRED /"
    echo "CRUCIBLE_COGNITO_REGION) before running this script."
    echo ""
    echo "To deploy WITHOUT auth deliberately, set"
    echo "CRUCIBLE_COGNITO_CONFIRM_DISABLE=1."
  } >&2
  exit 1
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

if [ "$AUTH_ENABLED" = "1" ]; then
  echo "==> Smoke check: confirming auth is actually enforced"
  # An unauthenticated request to an auth-required endpoint must be rejected.
  # This is the direct, cheap way to catch the exact regression this script
  # used to allow — auth silently coming back off after a deploy — as a loud
  # failure right here instead of a silent gap discovered later. A few
  # retries absorb the brief propagation/cold-start window right after
  # update-function-configuration and API Gateway creation.
  SMOKE_CODE="000"
  for _attempt in 1 2 3; do
    SMOKE_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
      "${URL}/api/memory/smoke-test-nonexistent-id" 2>/dev/null || echo "000")
    [ "$SMOKE_CODE" = "401" ] && break
    sleep 3
  done
  if [ "$SMOKE_CODE" = "401" ]; then
    echo "    OK — unauthenticated request correctly rejected (401)"
  else
    echo ""
    echo "############################################################"
    echo "#  DEPLOY SMOKE CHECK FAILED                                #"
    echo "#  Expected 401 from an auth-required endpoint, got:        #"
    echo "#    HTTP ${SMOKE_CODE}"
    echo "#  Auth may NOT be enforced on this deployment even though  #"
    echo "#  Cognito vars were provided. Do not treat this deploy as  #"
    echo "#  safe — investigate before relying on it.                 #"
    echo "############################################################"
    exit 1
  fi
else
  echo "==> Skipping auth smoke check (auth intentionally disabled via CRUCIBLE_COGNITO_CONFIRM_DISABLE)"
fi

echo ""
echo "Backend live: ${URL}"
echo "Health:       ${URL}/api/health"
