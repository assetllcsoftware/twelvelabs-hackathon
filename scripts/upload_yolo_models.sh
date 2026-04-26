#!/usr/bin/env bash
# Upload the trained YOLO-seg checkpoints from the sister pld-yolo project
# into the energy-hackathon videos bucket where the yolo-detect-worker
# expects to find them.
#
# Layout written:
#   s3://<bucket>/models/yolo/pldm-power-line/v1/best.pt
#   s3://<bucket>/models/yolo/airpelago-insulator-pole/v1/best.pt
#
# Usage:
#   S3_BUCKET=$(terraform -chdir=infra output -raw bucket_name) \
#     scripts/upload_yolo_models.sh
#
#   # or with explicit overrides:
#   S3_BUCKET=foo \
#   PLD_YOLO_DIR=../pld-yolo \
#     scripts/upload_yolo_models.sh
#
# Re-runs are idempotent: each upload is a single PUT under the same key,
# so the existing object is replaced. Re-running the worker with the same
# weights replays the same detections.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLD_YOLO_DIR="${PLD_YOLO_DIR:-$ROOT_DIR/../pld-yolo}"

if [[ -z "${S3_BUCKET:-}" ]]; then
    echo "S3_BUCKET env var is required (try: \$(terraform -chdir=infra output -raw bucket_name))" >&2
    exit 1
fi

if [[ ! -d "$PLD_YOLO_DIR" ]]; then
    echo "PLD_YOLO_DIR=$PLD_YOLO_DIR does not exist; pass PLD_YOLO_DIR=... to override" >&2
    exit 1
fi

# Map of "logical name" -> "directory under pld-yolo/runs/<*>" pattern.
# Mirrors the YOLO_MODELS JSON baked into the task definition.
declare -A MODELS=(
    ["pldm-power-line"]="pldm-subset2k-heavy"
    ["airpelago-insulator-pole"]="airpelago-yolo26s-seg"
)

resolve_weights() {
    local run_dir="$1"
    if [[ -f "$run_dir/weights/best.pt" ]]; then
        echo "$run_dir/weights/best.pt"; return
    fi
    if [[ -f "$run_dir/weights/last.pt" ]]; then
        echo "$run_dir/weights/last.pt"; return
    fi
    return 1
}

for name in "${!MODELS[@]}"; do
    run_name="${MODELS[$name]}"
    run_dir="$PLD_YOLO_DIR/runs/$run_name"
    if [[ ! -d "$run_dir" ]]; then
        echo "skip $name: no run directory at $run_dir" >&2
        continue
    fi
    if ! weights="$(resolve_weights "$run_dir")"; then
        echo "skip $name: no best.pt or last.pt under $run_dir/weights/" >&2
        continue
    fi
    target_key="models/yolo/$name/v1/best.pt"
    echo "==> uploading $name from $weights -> s3://$S3_BUCKET/$target_key"
    aws s3 cp "$weights" "s3://$S3_BUCKET/$target_key" \
        --content-type application/octet-stream \
        --metadata "source=$run_name"
done

echo "done."
