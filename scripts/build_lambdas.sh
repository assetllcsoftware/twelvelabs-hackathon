#!/usr/bin/env bash
# Build Lambda zip payloads under .build/lambda/<name>/ so Terraform's
# archive_file data source can package them. Pure Python deps only — pg8000
# is universal-wheel, no platform tagging required.
#
# Usage:
#   scripts/build_lambdas.sh                # build all
#   scripts/build_lambdas.sh start_clip_embed
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT_DIR/lambda"
OUT_DIR="$ROOT_DIR/.build/lambda"

names=("$@")
if [[ ${#names[@]} -eq 0 ]]; then
    mapfile -t names < <(find "$SRC_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)
fi

for name in "${names[@]}"; do
    src="$SRC_DIR/$name"
    out="$OUT_DIR/$name"

    if [[ ! -d "$src" ]]; then
        echo "build_lambdas: no source dir $src" >&2
        exit 1
    fi

    echo "==> building $name"
    rm -rf "$out"
    mkdir -p "$out"

    cp -a "$src"/*.py "$out"/

    if [[ -f "$src/requirements.txt" ]]; then
        pip install \
            --quiet \
            --no-cache-dir \
            --upgrade \
            --target "$out" \
            -r "$src/requirements.txt"
        # Keep *.dist-info — pg8000/scramp uses importlib.metadata at import.
        find "$out" -type d \( -name '__pycache__' -o -name '*.egg-info' \) -prune -exec rm -rf {} +
    fi
done

echo "lambdas built in $OUT_DIR"
