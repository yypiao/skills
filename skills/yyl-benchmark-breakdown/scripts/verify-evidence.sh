#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then
  echo "usage: bash scripts/verify-evidence.sh <run_dir>" >&2
  exit 2
fi
python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/verify_evidence.py" "$1"
