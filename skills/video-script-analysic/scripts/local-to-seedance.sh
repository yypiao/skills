#!/usr/bin/env bash
# Convert a local product/commerce video into a Seedance-ready prompt.
# Uses local Whisper/Vision/PIL plus deterministic local assembly.
# It does not call cloud LLMs or cloud VLMs.
#
# Usage:
#   bash scripts/local-to-seedance.sh "/path/to/input.mp4" [output.md]
#
# Optional environment:
#   PRODUCT_NAME="..."
#   PRODUCT_IMAGE="/path/to/product.png"
#   PRODUCT_NOTES="..."
#   OBSERVATIONS_FILE="/path/to/observations.json"
#   BRAND_ALIASES="原品牌,原商品名"
#   SIMPLE_PROMPT="/path/to/seedance-prompt.txt"
#   RESULT_JSON="/path/to/result.json"
#   RUN_DIR="/path/to/evidence"
#
# For URL inputs, ordinary fetch/platform APIs are allowed by the skill, but
# cloud LLM/VLM services remain disabled for analysis and rewriting.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: bash scripts/local-to-seedance.sh <video> [output.md]" >&2
  exit 1
fi

INPUT="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${RUN_DIR:-$PWD/benchmarks/_evidence/local-${RUN_ID}}"
OUTPUT="${2:-}"

if [[ -f "$INPUT" ]]; then
  bash "$SCRIPT_DIR/check-deps.sh" --local-video
else
  bash "$SCRIPT_DIR/check-deps.sh"
fi

bash "$SCRIPT_DIR/prepare-assets.sh" "$INPUT" "$RUN_DIR"

if [[ -z "$OUTPUT" ]]; then
  safe_input="$(basename "$INPUT")"
  safe_input="${safe_input%.*}"
  safe_input="$(printf '%s' "$safe_input" | tr -c '[:alnum:]' '-' | tr -s '-' | sed 's/^-//;s/-$//' | cut -c1-40)"
  OUTPUT="$PWD/benchmarks/local-$(date +%Y-%m-%d)-${safe_input}-seedance.md"
fi
mkdir -p "$(dirname "$OUTPUT")"

if [[ -n "${OBSERVATIONS_FILE:-}" && ! -f "$OBSERVATIONS_FILE" ]]; then
  echo "OBSERVATIONS_FILE not found: $OBSERVATIONS_FILE" >&2
  exit 1
fi

args=(
  "$RUN_DIR"
  --output "$OUTPUT"
)
[[ -n "${PRODUCT_NAME:-}" ]] && args+=(--product-name "$PRODUCT_NAME")
[[ -n "${PRODUCT_IMAGE:-}" ]] && args+=(--product-image "$PRODUCT_IMAGE")
[[ -n "${PRODUCT_NOTES:-}" ]] && args+=(--product-notes "$PRODUCT_NOTES")
[[ -n "${OBSERVATIONS_FILE:-}" ]] && args+=(--observations-file "$OBSERVATIONS_FILE")
[[ -n "${BRAND_ALIASES:-}" ]] && args+=(--brand-aliases "$BRAND_ALIASES")

SIMPLE_PROMPT="${SIMPLE_PROMPT:-${OUTPUT%.md}-prompt.txt}"
RESULT_JSON="${RESULT_JSON:-${OUTPUT%.md}.json}"
args+=(--simple-output "$SIMPLE_PROMPT")
args+=(--json-output "$RESULT_JSON")

python3 "$SCRIPT_DIR/build_seedance_prompt.py" "${args[@]}"

echo "evidence: $RUN_DIR"
echo "seedance_prompt: $OUTPUT"
echo "simple_prompt: $SIMPLE_PROMPT"
echo "result_json: $RESULT_JSON"
