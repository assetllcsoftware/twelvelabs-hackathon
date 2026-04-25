#!/usr/bin/env bash
set -u

REGION="${AWS_DEFAULT_REGION:-us-east-1}"

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI not found on PATH" >&2
  exit 1
fi

if [[ -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
  echo "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be exported first" >&2
  exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

printf 'AWS access probe\n'
printf 'Region: %s\n\n' "$REGION"

run_probe() {
  local label="$1"
  shift
  local out="$TMPDIR/${label//[^A-Za-z0-9_.-]/_}.out"
  local err="$TMPDIR/${label//[^A-Za-z0-9_.-]/_}.err"

  if "$@" >"$out" 2>"$err"; then
    local bytes
    bytes="$(wc -c <"$out" | tr -d ' ')"
    printf '[OK]     %s (%s bytes)\n' "$label" "$bytes"
    if [[ "$bytes" != "0" ]]; then
      sed 's/^/         /' "$out" | head -n 20
    fi
  else
    local status=$?
    printf '[DENIED] %s (exit %s)\n' "$label" "$status"
    sed 's/^/         /' "$err" | head -n 6
  fi
  printf '\n'
}

run_probe "STS caller identity" \
  aws sts get-caller-identity --output json

run_probe "S3 list buckets" \
  aws s3api list-buckets --output json

run_probe "EC2 describe regions" \
  aws ec2 describe-regions --region "$REGION" --output json

run_probe "EC2 describe instances" \
  aws ec2 describe-instances --region "$REGION" --max-results 20 --output json

run_probe "EC2 describe VPCs" \
  aws ec2 describe-vpcs --region "$REGION" --output json

run_probe "ECS list clusters" \
  aws ecs list-clusters --region "$REGION" --output json

run_probe "Lambda list functions" \
  aws lambda list-functions --region "$REGION" --max-items 20 --output json

run_probe "Bedrock list foundation models" \
  aws bedrock list-foundation-models --region "$REGION" --output json

run_probe "Bedrock Runtime minimal invoke permission check" \
  aws bedrock-runtime converse \
    --region "$REGION" \
    --model-id amazon.nova-lite-v1:0 \
    --messages '[{"role":"user","content":[{"text":"Reply with ok."}]}]' \
    --inference-config '{"maxTokens":8,"temperature":0}' \
    --output json

run_probe "OpenSearch list domains" \
  aws opensearch list-domain-names --region "$REGION" --output json

run_probe "OpenSearch Serverless list collections" \
  aws opensearchserverless list-collections --region "$REGION" --output json

run_probe "IAM get user" \
  aws iam get-user --output json

run_probe "CloudWatch Logs describe log groups" \
  aws logs describe-log-groups --region "$REGION" --limit 20 --output json

run_probe "ECR describe repositories" \
  aws ecr describe-repositories --region "$REGION" --max-items 20 --output json

run_probe "CloudFormation list stacks" \
  aws cloudformation list-stacks \
    --region "$REGION" \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE ROLLBACK_COMPLETE \
    --output json
