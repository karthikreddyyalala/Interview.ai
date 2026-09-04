#!/usr/bin/env bash
#
# Deploys the frontend: builds the SPA pointed at the live API, pushes it to a
# private S3 bucket, and serves it over HTTPS via CloudFront (Origin Access
# Control). HTTPS is required so the in-browser mic (voice mode) works.
# Fully idempotent: reuses the existing CloudFront distribution (matched by the
# "crucible frontend" comment) so the public URL stays stable across runs, and
# invalidates the cache so a new build shows immediately.
#
# Usage:
#   CRUCIBLE_API_BASE="https://xxxx.execute-api.us-west-2.amazonaws.com" bash deploy/deploy-frontend.sh
set -euo pipefail

ACCOUNT="557690618983"
REGION="us-west-2"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUCKET="crucible-frontend-${ACCOUNT}"
API_BASE="${CRUCIBLE_API_BASE:?Set CRUCIBLE_API_BASE to the Lambda Function URL}"

echo "==> Building frontend against ${API_BASE}"
cd "$ROOT"
# Cognito is a live access-control boundary, not a cosmetic feature: set
# CRUCIBLE_COGNITO_USER_POOL_ID and CRUCIBLE_COGNITO_CLIENT_ID in your OWN
# shell (never commit these) so the built bundle can sign users in. Leaving
# them unset silently ships a build with login disabled (AUTH_CONFIGURED
# is false in src/lib/auth.ts), which the app itself never flags — so this
# hard-fails by default. To build WITHOUT auth on purpose, set
# CRUCIBLE_COGNITO_CONFIRM_DISABLE=1.
if [ -n "${CRUCIBLE_COGNITO_USER_POOL_ID:-}" ] && [ -n "${CRUCIBLE_COGNITO_CLIENT_ID:-}" ]; then
  echo "==> Cognito auth: ENABLED (user pool + client id provided)"
elif [ "${CRUCIBLE_COGNITO_CONFIRM_DISABLE:-}" = "1" ]; then
  echo ""
  echo "############################################################"
  echo "#  WARNING: BUILDING WITH COGNITO LOGIN DISABLED            #"
  echo "#  CRUCIBLE_COGNITO_CONFIRM_DISABLE=1 is set. This build    #"
  echo "#  will ship with no working sign-in (AUTH_CONFIGURED       #"
  echo "#  false). Proceed only if you intend that for this deploy. #"
  echo "############################################################"
  echo ""
else
  {
    echo ""
    echo "ERROR: CRUCIBLE_COGNITO_USER_POOL_ID and CRUCIBLE_COGNITO_CLIENT_ID are"
    echo "not both set."
    echo ""
    echo "The built bundle bakes these in at build time (src/lib/auth.ts). Running"
    echo "this now would SILENTLY SHIP a build where login is disabled, with no"
    echo "runtime warning — AUTH_CONFIGURED just becomes false."
    echo ""
    echo "Fix: export CRUCIBLE_COGNITO_USER_POOL_ID and CRUCIBLE_COGNITO_CLIENT_ID"
    echo "in this shell before running this script."
    echo ""
    echo "To build WITHOUT auth deliberately, set CRUCIBLE_COGNITO_CONFIRM_DISABLE=1."
  } >&2
  exit 1
fi
VITE_USE_MOCK=false VITE_API_BASE="$API_BASE" \
  VITE_COGNITO_USER_POOL_ID="${CRUCIBLE_COGNITO_USER_POOL_ID:-}" \
  VITE_COGNITO_CLIENT_ID="${CRUCIBLE_COGNITO_CLIENT_ID:-}" \
  npm run build >/dev/null
echo "    built dist/"

echo "==> Private S3 bucket ${BUCKET}"
aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || \
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
aws s3 sync "$ROOT/dist" "s3://${BUCKET}" --delete >/dev/null
echo "    synced dist -> s3://${BUCKET}"

# Origin Access Control so only CloudFront can read the bucket.
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='crucible-oac'].Id | [0]" --output text 2>/dev/null || echo "None")
if [ "$OAC_ID" = "None" ] || [ -z "$OAC_ID" ]; then
  OAC_ID=$(aws cloudfront create-origin-access-control --origin-access-control-config \
    "Name=crucible-oac,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
    --query "OriginAccessControl.Id" --output text)
fi
echo "==> OAC ${OAC_ID}"

CALLER_REF="crucible-$(date +%s)"
DIST_CONFIG=$(cat <<JSON
{
  "CallerReference": "${CALLER_REF}",
  "Comment": "crucible frontend",
  "Enabled": true,
  "DefaultRootObject": "index.html",
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "s3-crucible",
      "DomainName": "${BUCKET}.s3.${REGION}.amazonaws.com",
      "OriginAccessControlId": "${OAC_ID}",
      "S3OriginConfig": { "OriginAccessIdentity": "" }
    }]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "s3-crucible",
    "ViewerProtocolPolicy": "redirect-to-https",
    "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
    "Compress": true
  },
  "CustomErrorResponses": {
    "Quantity": 1,
    "Items": [{
      "ErrorCode": 403,
      "ResponseCode": "200",
      "ResponsePagePath": "/index.html",
      "ErrorCachingMinTTL": 10
    }]
  }
}
JSON
)

# Reuse an existing distribution (stable public URL) or create one on first run.
DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='crucible frontend'].Id | [0]" \
  --output text 2>/dev/null || echo "None")

if [ "$DIST_ID" = "None" ] || [ -z "$DIST_ID" ]; then
  echo "==> Creating CloudFront distribution (first run)"
  DIST_JSON=$(aws cloudfront create-distribution --distribution-config "$DIST_CONFIG")
  DIST_ID=$(echo "$DIST_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['Distribution']['Id'])")
  DOMAIN=$(echo "$DIST_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['Distribution']['DomainName'])")

  echo "==> Bucket policy: allow this distribution to read"
  aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Sid\": \"AllowCloudFront\",
      \"Effect\": \"Allow\",
      \"Principal\": { \"Service\": \"cloudfront.amazonaws.com\" },
      \"Action\": \"s3:GetObject\",
      \"Resource\": \"arn:aws:s3:::${BUCKET}/*\",
      \"Condition\": { \"StringEquals\": { \"AWS:SourceArn\": \"arn:aws:cloudfront::${ACCOUNT}:distribution/${DIST_ID}\" } }
    }]
  }" >/dev/null
else
  DOMAIN=$(aws cloudfront get-distribution --id "$DIST_ID" \
    --query "Distribution.DomainName" --output text)
  echo "==> Reusing existing distribution ${DIST_ID} (${DOMAIN})"
fi

echo "==> Invalidating CloudFront cache so the new build shows immediately"
if aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" \
     --query "Invalidation.Id" --output text 2>/dev/null; then
  echo "    invalidation created"
else
  echo "    WARNING: could not create invalidation (missing cloudfront:CreateInvalidation)."
  echo "    New assets (hashed filenames) serve immediately; index.html may take up"
  echo "    to the cache TTL to refresh. Add the permission or invalidate in the console."
fi

echo ""
echo "Frontend deployed to the stable URL:"
echo "  https://${DOMAIN}"
