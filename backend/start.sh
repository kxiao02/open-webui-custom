#!/usr/bin/env bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd "$SCRIPT_DIR" || exit

PYTHON_CMD=$(command -v python3 || command -v python)

ensure_nltk_resources() {
    echo "Ensuring NLTK resources for document extraction..."
    "$PYTHON_CMD" -m open_webui.utils.nltk_preload
}

ensure_nltk_resources

# Add conditional Playwright browser installation
if [[ "${WEB_LOADER_ENGINE,,}" == "playwright" ]]; then
    if [[ -z "${PLAYWRIGHT_WS_URL}" ]]; then
        echo "Installing Playwright browsers..."
        playwright install chromium
        playwright install-deps chromium
    fi
fi

if [ -n "${WEBUI_SECRET_KEY_FILE}" ]; then
    KEY_FILE="${WEBUI_SECRET_KEY_FILE}"
else
    KEY_FILE=".webui_secret_key"
fi

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
if [[ "${STRICT_EXTERNAL_STATE,,}" == "true" ]] && test "$WEBUI_SECRET_KEY $WEBUI_JWT_SECRET_KEY" = " "; then
  echo "STRICT_EXTERNAL_STATE requires WEBUI_SECRET_KEY or WEBUI_JWT_SECRET_KEY to be set via environment."
  exit 1
fi
if test "$WEBUI_SECRET_KEY $WEBUI_JWT_SECRET_KEY" = " "; then
  echo "Loading WEBUI_SECRET_KEY from file, not provided as an environment variable."

  if ! [ -e "$KEY_FILE" ]; then
    echo "Generating WEBUI_SECRET_KEY"
    # Generate a random value to use as a WEBUI_SECRET_KEY in case the user didn't provide one.
    echo $(head -c 12 /dev/random | base64) > "$KEY_FILE"
  fi

  echo "Loading WEBUI_SECRET_KEY from $KEY_FILE"
  WEBUI_SECRET_KEY=$(cat "$KEY_FILE")
fi

if [[ "${USE_OLLAMA_DOCKER,,}" == "true" ]]; then
    echo "USE_OLLAMA is set to true, starting ollama serve."
    ollama serve &
fi

if [[ "${USE_CUDA_DOCKER,,}" == "true" ]]; then
  echo "CUDA is enabled, appending LD_LIBRARY_PATH to include torch/cudnn & cublas libraries."
  export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/usr/local/lib/python3.11/site-packages/torch/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib"
fi

# Check if SPACE_ID is set, if so, configure for space
if [ -n "$SPACE_ID" ]; then
  echo "Configuring for HuggingFace Space deployment"
  if [ -n "$ADMIN_USER_EMAIL" ] && [ -n "$ADMIN_USER_PASSWORD" ]; then
    echo "Admin user configured, creating"
    WEBUI_SECRET_KEY="$WEBUI_SECRET_KEY" uvicorn open_webui.main:app --host "$HOST" --port "$PORT" --forwarded-allow-ips '*' &
    webui_pid=$!
    echo "Waiting for webui to start..."
    while ! curl -s "http://localhost:${PORT}/health" > /dev/null; do
      sleep 1
    done
    echo "Creating admin user..."
    curl \
      -X POST "http://localhost:${PORT}/api/v1/auths/signup" \
      -H "accept: application/json" \
      -H "Content-Type: application/json" \
      -d "{ \"email\": \"${ADMIN_USER_EMAIL}\", \"password\": \"${ADMIN_USER_PASSWORD}\", \"name\": \"Admin\" }"
    echo "Shutting down webui..."
    kill $webui_pid
  fi

  export WEBUI_URL=${SPACE_HOST}
fi

if [[ "${STORAGE_PROVIDER,,}" == "s3" ]] && [ -n "${S3_BUCKET_NAME}" ] && [ -n "${S3_ENDPOINT_URL}" ]; then
    echo "Ensuring S3 bucket exists: ${S3_BUCKET_NAME}"
    "$PYTHON_CMD" - <<'PY'
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

bucket_name = os.environ["S3_BUCKET_NAME"]
endpoint_url = os.environ["S3_ENDPOINT_URL"]
region_name = os.environ.get("S3_REGION_NAME") or None
access_key = os.environ.get("S3_ACCESS_KEY_ID") or None
secret_key = os.environ.get("S3_SECRET_ACCESS_KEY") or None
addressing_style = os.environ.get("S3_ADDRESSING_STYLE") or None

kwargs = {
    "region_name": region_name,
    "endpoint_url": endpoint_url,
    "config": Config(
        s3={"addressing_style": addressing_style},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    ),
}
if access_key and secret_key:
    kwargs["aws_access_key_id"] = access_key
    kwargs["aws_secret_access_key"] = secret_key

client = boto3.client("s3", **kwargs)

try:
    client.head_bucket(Bucket=bucket_name)
except ClientError as exc:
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = str(exc.response.get("Error", {}).get("Code", ""))
    if status == 404 or code in {"404", "NoSuchBucket", "NotFound"}:
        create_kwargs = {"Bucket": bucket_name}
        if (
            region_name
            and region_name != "us-east-1"
            and "amazonaws.com" in endpoint_url
        ):
            create_kwargs["CreateBucketConfiguration"] = {
                "LocationConstraint": region_name
            }
        client.create_bucket(**create_kwargs)
        print(f"Created bucket: {bucket_name}")
    else:
        raise
else:
    print(f"Bucket already exists: {bucket_name}")
PY
fi

UVICORN_WORKERS="${UVICORN_WORKERS:-1}"

# If script is called with arguments, use them; otherwise use default workers
if [ "$#" -gt 0 ]; then
    ARGS=("$@")
else
    ARGS=(--workers "$UVICORN_WORKERS")
fi

# Run uvicorn
WEBUI_SECRET_KEY="$WEBUI_SECRET_KEY" exec "$PYTHON_CMD" -m uvicorn open_webui.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --forwarded-allow-ips '*' \
    "${ARGS[@]}"
