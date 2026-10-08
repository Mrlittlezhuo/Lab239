#!/usr/bin/env bash
set -euo pipefail

FASTJEV_PYTHON="${FASTJEV_PYTHON:-/home/lab239/anaconda3/envs/FastJev/bin/python}"
FASTJEV_MODEL_PATH="${FASTJEV_MODEL_PATH:-/home/lab239/chenzhuo/models/fastjev/Qwen_Qwen3.5-4B-Q4_K_M.gguf}"
FASTJEV_REVISION="${FASTJEV_REVISION:-851bf6e806efd8d0a36b00ddf55e13cccb7b8cd0a}"
export CUDA_VISIBLE_DEVICES="${FASTJEV_CUDA_DEVICE:-1}"

exec "$FASTJEV_PYTHON" "$(dirname "$0")/scripts/fastjev_server.py" \
  --model-path "$FASTJEV_MODEL_PATH" \
  --revision "$FASTJEV_REVISION" \
  --host "${FASTJEV_HOST:-127.0.0.1}" \
  --port "${FASTJEV_PORT:-8811}"
