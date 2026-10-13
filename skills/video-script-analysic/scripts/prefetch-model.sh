#!/usr/bin/env bash
# Reliably pre-download an OpenAI Whisper model with resume + checksum verification.
set -euo pipefail

MODEL="${1:-${WHISPER_MODEL:-medium}}"
WHISPER_BIN="${WHISPER_BIN:-$(command -v whisper || true)}"
MODEL_ROOT="${WHISPER_MODEL_ROOT:-$HOME/.cache/whisper}"

if [[ -z "$WHISPER_BIN" ]]; then
  echo "✗ whisper command not found. Install openai-whisper first; see INSTALL.md." >&2
  exit 1
fi

# Resolve the Python executable backing the whisper CLI, because /usr/bin/python3
# generally does not contain the Whisper package.
PYTHON_BIN="${WHISPER_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  shebang="$(head -1 "$WHISPER_BIN" | sed 's/^#!//' || true)"
  candidate="${shebang%% *}"
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    PYTHON_BIN="$candidate"
  else
    echo "✗ cannot determine Python for whisper: $WHISPER_BIN" >&2
    exit 1
  fi
fi

# Explicit proxy environment variables win; otherwise auto-detect common Clash port.
if [[ -z "${HTTPS_PROXY:-}${HTTP_PROXY:-}" ]] && command -v nc >/dev/null 2>&1 && nc -z 127.0.0.1 7897 >/dev/null 2>&1; then
  export HTTPS_PROXY="http://127.0.0.1:7897"
  export HTTP_PROXY="http://127.0.0.1:7897"
  echo "→ using local proxy: $HTTPS_PROXY"
fi

INFO="$($PYTHON_BIN - "$MODEL" <<'PY'
import sys
from whisper import _MODELS
name = sys.argv[1]
if name not in _MODELS:
    raise SystemExit(f"unknown Whisper model: {name}")
url = _MODELS[name]
expected_sha256 = url.rstrip('/').split('/')[-2]
print(url)
print(expected_sha256)
PY
)"
URL="$(printf '%s\n' "$INFO" | sed -n '1p')"
EXPECTED_SHA256="$(printf '%s\n' "$INFO" | sed -n '2p')"

mkdir -p "$MODEL_ROOT"
TARGET="$MODEL_ROOT/$MODEL.pt"
PART="$MODEL_ROOT/$MODEL.pt.part"

if [[ -f "$TARGET" ]]; then
  ACTUAL="$(shasum -a 256 "$TARGET" | awk '{print $1}')"
  if [[ "$ACTUAL" == "$EXPECTED_SHA256" ]]; then
    echo "✓ model already ready: $MODEL ($TARGET)"
    exit 0
  fi
  echo "! existing model has wrong checksum; moving it aside: $TARGET"
  mv "$TARGET" "$TARGET.invalid.$(date +%Y%m%d-%H%M%S)"
fi

echo "→ model: $MODEL"
echo "→ target: $TARGET"
echo "→ expected SHA256: $EXPECTED_SHA256"
REMOTE_SIZE="$(curl -sIL --connect-timeout 20 "$URL" | awk 'BEGIN{IGNORECASE=1} /^content-length:/ {v=$0} END{sub(/^[^:]*:[[:space:]]*/, "", v); print v}')"
if [[ -n "$REMOTE_SIZE" ]]; then
  echo "→ remote size: $REMOTE_SIZE bytes"
fi

echo "→ downloading with resume support; slow transfers are tolerated and restarted..."
ATTEMPT=1
MAX_ATTEMPTS="${PREFETCH_MAX_ATTEMPTS:-80}"
while [[ "$ATTEMPT" -le "$MAX_ATTEMPTS" ]]; do
  echo "→ attempt $ATTEMPT/$MAX_ATTEMPTS"
  if curl --fail --location --continue-at - \
      --connect-timeout 20 \
      --speed-time 60 --speed-limit 1024 \
      --progress-bar \
      "$URL" --output "$PART"; then
    break
  fi
  RC=$?
  echo "! curl exited with $RC; resume after 3 seconds if attempts remain"
  ATTEMPT=$((ATTEMPT + 1))
  sleep 3
done

if [[ "$ATTEMPT" -gt "$MAX_ATTEMPTS" ]]; then
  echo "✗ download did not complete after $MAX_ATTEMPTS attempts; partial retained: $PART" >&2
  exit 1
fi

if [[ -n "$REMOTE_SIZE" ]]; then
  ACTUAL_SIZE="$(stat -f%z "$PART")"
  if [[ "$ACTUAL_SIZE" != "$REMOTE_SIZE" ]]; then
    echo "✗ downloaded size $ACTUAL_SIZE does not match remote size $REMOTE_SIZE" >&2
    exit 1
  fi
fi

ACTUAL="$(shasum -a 256 "$PART" | awk '{print $1}')"
if [[ "$ACTUAL" != "$EXPECTED_SHA256" ]]; then
  echo "✗ checksum mismatch after download" >&2
  echo "  expected: $EXPECTED_SHA256" >&2
  echo "  actual:   $ACTUAL" >&2
  echo "  partial file retained for resume: $PART" >&2
  exit 1
fi

mv "$PART" "$TARGET"
echo "✓ model ready: $MODEL ($TARGET)"
