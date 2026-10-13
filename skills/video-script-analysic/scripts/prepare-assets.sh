#!/usr/bin/env bash
# Prepare evidence assets for yyl-benchmark-breakdown.
# Output: metadata.json, video/audio/transcript, contact sheets, and manifest.txt.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/prepare-assets.sh "<url-or-local-video>" [output_dir]

Examples:
  bash scripts/prepare-assets.sh "https://www.douyin.com/video/..."
  bash scripts/prepare-assets.sh "./data/7686799477084015905.mp4"

Environment:
  DOUYIN_API_BASE       Default: http://localhost:80
  XHS_API_BASE          Default: http://127.0.0.1:5556
  XHS_CONTAINER_NAME    Default: xhs-downloader-api
  XHS_DOWNLOAD_DIR      Default: inferred from Docker mount, then $HOME/.xhs-downloader/Volume/Download
  BENCHMARK_WORKDIR     Default: /tmp/yyl-benchmark
  WHISPER_BIN           Default: whisper on PATH, then $HOME/.whisper-venv/bin/whisper
  WHISPER_MODEL         Default: auto (use complete cached medium, otherwise complete cached small, otherwise medium)
  WHISPER_LANGUAGE      Default: auto (omit Whisper language flag; use Chinese only for known Chinese originals)
  WHISPER_INITIAL_PROMPT Optional; empty by default. Do not force terminology prompts.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 0
fi

INPUT="$1"
URL="$1"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
WORK_ROOT="${BENCHMARK_WORKDIR:-/tmp/yyl-benchmark}"
RUN_DIR="${2:-$WORK_ROOT/$RUN_ID}"
DOUYIN_API_BASE="${DOUYIN_API_BASE:-http://localhost:80}"
XHS_API_BASE="${XHS_API_BASE:-http://127.0.0.1:5556}"
XHS_CONTAINER_NAME="${XHS_CONTAINER_NAME:-xhs-downloader-api}"
WHISPER_MODEL="${WHISPER_MODEL:-auto}"
WHISPER_LANGUAGE="${WHISPER_LANGUAGE:-auto}"
WHISPER_INITIAL_PROMPT="${WHISPER_INITIAL_PROMPT:-}"

infer_xhs_download_dir() {
  if [[ -n "${XHS_DOWNLOAD_DIR:-}" ]]; then
    printf '%s\n' "$XHS_DOWNLOAD_DIR"
    return 0
  fi

  if command -v docker >/dev/null 2>&1; then
    local volume_dir
    volume_dir="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/Volume"}}{{.Source}}{{end}}{{end}}' "$XHS_CONTAINER_NAME" 2>/dev/null || true)"
    if [[ -n "$volume_dir" ]]; then
      printf '%s\n' "$volume_dir/Download"
      return 0
    fi
  fi

  printf '%s\n' "$HOME/.xhs-downloader/Volume/Download"
}

XHS_DOWNLOAD_DIR="$(infer_xhs_download_dir)"

mkdir -p "$RUN_DIR/frames" "$RUN_DIR/xhs_images"
MANIFEST="$RUN_DIR/manifest.txt"
METADATA="$RUN_DIR/metadata.json"

log() { printf '%s\n' "$*" | tee -a "$MANIFEST" >&2; }
need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "missing dependency: $1"
    exit 1
  fi
}

if [[ -f "$INPUT" ]]; then
  need ffmpeg
  need ffprobe
  need python3
else
  need curl
  need ffmpeg
  need ffprobe
  need python3
fi

detect_platform() {
  case "$URL" in
    *xiaohongshu.com*|*xhslink.com*) echo "xhs" ;;
    *douyin.com*|*v.douyin.com*) echo "douyin" ;;
    *tiktok.com*) echo "tiktok" ;;
    *bilibili.com*|*b23.tv*) echo "bilibili" ;;
    *youtube.com*|*youtu.be*) echo "youtube" ;;
    *mp.weixin.qq.com*) echo "weixin" ;;
    *) echo "unknown" ;;
  esac
}

json_payload() {
  local download="${1:-true}"
  python3 - "$URL" "$download" <<'PY'
import json, sys
download = sys.argv[2].lower() == "true"
print(json.dumps({"url": sys.argv[1], "download": download, "skip": False}, ensure_ascii=False))
PY
}

xhs_settings_file() {
  local volume_dir
  volume_dir="$(dirname "$XHS_DOWNLOAD_DIR" 2>/dev/null || true)"
  if [[ -n "$volume_dir" && -f "$volume_dir/settings.json" ]]; then
    printf '%s\n' "$volume_dir/settings.json"
    return 0
  fi

  if command -v docker >/dev/null 2>&1; then
    local mounted_volume
    mounted_volume="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/Volume"}}{{.Source}}{{end}}{{end}}' "$XHS_CONTAINER_NAME" 2>/dev/null || true)"
    if [[ -n "$mounted_volume" && -f "$mounted_volume/settings.json" ]]; then
      printf '%s\n' "$mounted_volume/settings.json"
      return 0
    fi
  fi

  return 1
}

xhs_cookie_configured() {
  local settings
  settings="$(xhs_settings_file || true)"
  [[ -n "$settings" ]] || return 1
  python3 - "$settings" <<'PY'
import json, sys
try:
    cookie = json.load(open(sys.argv[1], encoding="utf-8")).get("cookie") or ""
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if cookie.strip() else 1)
PY
}

warn_xhs_cookie_if_needed() {
  if ! xhs_cookie_configured; then
    log "warning: XHS API returned no data and XHS-Downloader cookie is empty"
    log "warning: official docs say cookie is optional, but configure/update Volume/settings.json cookie when XHS functions are abnormal"
  fi
}

extract_video_url() {
  python3 - "$METADATA" <<'PY'
import json, sys
from collections.abc import Mapping, Sequence

with open(sys.argv[1], "r", encoding="utf-8") as f:
    root = json.load(f)

def walk(node):
    if isinstance(node, Mapping):
        if "download_addr" in node:
            yield node["download_addr"]
        for key in ("play_addr", "video", "video_url", "url", "urls"):
            if key in node:
                yield node[key]
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for value in node:
            yield from walk(value)

def urls_from(node):
    if isinstance(node, str) and node.startswith(("http://", "https://")):
        yield node
    elif isinstance(node, Mapping):
        for key in ("url_list", "urls"):
            value = node.get(key)
            if isinstance(value, list):
                for item in value:
                    yield from urls_from(item)
        for key in ("url", "href", "download_url", "master_url"):
            yield from urls_from(node.get(key))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for item in node:
            yield from urls_from(item)

seen = set()
for candidate in walk(root):
    for url in urls_from(candidate):
        if url not in seen:
            seen.add(url)
            print(url)
            raise SystemExit(0)
raise SystemExit(1)
PY
}

download_video_from_url() {
  local video_url="$1"
  local referer="$2"
  curl -L -A "Mozilla/5.0" -H "Referer: $referer" "$video_url" -o "$RUN_DIR/video.mp4"
}

select_python_with_pil() {
  if python3 -c 'import PIL' >/dev/null 2>&1; then
    command -v python3
  elif [[ -x /usr/bin/python3 ]] && /usr/bin/python3 -c 'import PIL' >/dev/null 2>&1; then
    echo /usr/bin/python3
  else
    echo ""
  fi
}

PYTHON_WITH_PIL="$(select_python_with_pil)"

select_whisper_bin() {
  if [[ -n "${WHISPER_BIN:-}" && -x "$WHISPER_BIN" ]]; then
    printf '%s\n' "$WHISPER_BIN"
  elif command -v whisper >/dev/null 2>&1; then
    command -v whisper
  elif [[ -x "$HOME/.whisper-venv/bin/whisper" ]]; then
    printf '%s\n' "$HOME/.whisper-venv/bin/whisper"
  fi
}

WHISPER_BIN_RESOLVED="$(select_whisper_bin || true)"

model_is_cached() {
  local model="$1"
  local file="$HOME/.cache/whisper/$model.pt"
  [[ -f "$file" ]] || return 1
  local size
  size="$(stat -f%z "$file" 2>/dev/null || echo 0)"
  case "$model" in
    medium) [[ "$size" -gt 1000000000 ]] ;;
    small) [[ "$size" -gt 300000000 ]] ;;
    *) [[ "$size" -gt 1000000 ]] ;;
  esac
}

resolve_whisper_model() {
  if [[ "$WHISPER_MODEL" != "auto" ]]; then
    printf '%s\n' "$WHISPER_MODEL"
  elif model_is_cached medium; then
    printf 'medium\n'
  elif model_is_cached small; then
    printf 'small\n'
  else
    # 离线安全：auto 模式不返回未缓存模型，避免 Whisper 自动联网下载。
    true
  fi
}

WHISPER_MODEL_RESOLVED="$(resolve_whisper_model)"

srt_to_plain_text() {
  local srt="$1" txt="$2"
  python3 - "$srt" "$txt" <<'PY'
import re, sys
from pathlib import Path
src, dst = map(Path, sys.argv[1:])
lines = []
for raw in src.read_text(encoding='utf-8', errors='ignore').splitlines():
    line = raw.strip()
    if not line or line.isdigit():
        continue
    if re.match(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}", line):
        continue
    lines.append(line)
dst.write_text('\n'.join(lines).strip() + ('\n' if lines else ''), encoding='utf-8')
PY
}

transcribe_audio() {
  if [[ -s "$RUN_DIR/audio.txt" ]]; then
    log "transcript: $RUN_DIR/audio.txt (reused)"
    return 0
  fi

  if [[ -z "$WHISPER_BIN_RESOLVED" ]]; then
    log "warning: whisper executable not found; audio transcript skipped"
    log "hint: install with INSTALL.md or set WHISPER_BIN to the whisper executable"
    return 0
  fi

  log "transcribing with: $WHISPER_BIN_RESOLVED model=$WHISPER_MODEL_RESOLVED language=$WHISPER_LANGUAGE"
  local -a whisper_args
  whisper_args=(
    "$RUN_DIR/audio.wav"
    --model "$WHISPER_MODEL_RESOLVED"
    --output_dir "$RUN_DIR"
    --output_format srt
    --task transcribe
    --fp16 False
    --condition_on_previous_text False
  )
  if [[ "$WHISPER_LANGUAGE" != "auto" && -n "$WHISPER_LANGUAGE" ]]; then
    whisper_args+=(--language "$WHISPER_LANGUAGE")
  fi
  if [[ -n "$WHISPER_INITIAL_PROMPT" ]]; then
    whisper_args+=(--initial_prompt "$WHISPER_INITIAL_PROMPT")
  fi
  "$WHISPER_BIN_RESOLVED" "${whisper_args[@]}"

  if [[ -f "$RUN_DIR/audio.srt" ]]; then
    srt_to_plain_text "$RUN_DIR/audio.srt" "$RUN_DIR/audio.txt"
  fi
  if [[ "$WHISPER_MODEL_RESOLVED" == "small" || "$WHISPER_MODEL_RESOLVED" == "base" || "$WHISPER_MODEL_RESOLVED" == "tiny" ]]; then
    log "quality warning: $WHISPER_MODEL_RESOLVED is a fallback model; resolve terminology using ocr.md/burned-in subtitles"
  fi
  if [[ -z "$WHISPER_INITIAL_PROMPT" ]]; then
    log "transcript note: no forced initial prompt (safer for AI-dubbed/translated videos)"
  fi
}

ensure_vision_ocr() {
  local cache_dir="$HOME/.cache/yyl-benchmark"
  local binary="$cache_dir/vision-ocr"
  mkdir -p "$cache_dir"
  if [[ -x "$binary" ]]; then
    printf '%s\n' "$binary"
  elif [[ "$(uname -s)" == "Darwin" ]] && command -v swiftc >/dev/null 2>&1; then
    if swiftc "$SCRIPT_DIR/ocr.swift" -o "$binary" >/dev/null 2>&1; then
      printf '%s\n' "$binary"
    else
      return 1
    fi
  else
    return 1
  fi
}

run_visual_ocr() {
  local ocr_bin raw_ocr="$RUN_DIR/ocr_raw.txt" ocr_md="$RUN_DIR/ocr.md"
  OCR_IMAGES=()
  while IFS= read -r file; do
    OCR_IMAGES+=("$file")
  done < <(find "$RUN_DIR/frames" -maxdepth 1 -type f -name 's_*.jpg' | sort)
  [[ ${#OCR_IMAGES[@]} -gt 0 ]] || return 0

  if ocr_bin="$(ensure_vision_ocr)"; then
    "$ocr_bin" "${OCR_IMAGES[@]}" > "$raw_ocr" || true
    python3 "$SCRIPT_DIR/build_ocr_report.py" "$raw_ocr" "$ocr_md"
    if [[ -s "$ocr_md" ]]; then
      log "visual OCR: $ocr_md"
    else
      log "warning: Vision OCR produced no text"
    fi
  else
    log "warning: macOS Vision OCR unavailable; burned-in subtitles were not extracted"
  fi
}

run_visual_metrics() {
  if [[ -z "$PYTHON_WITH_PIL" ]]; then
    log "warning: Python PIL unavailable; visual metrics skipped"
    return 0
  fi
  "$PYTHON_WITH_PIL" "$SCRIPT_DIR/analyze_frames.py" \
    "$RUN_DIR/frames" "$RUN_DIR/visual_metrics.json" "$RUN_DIR/visual_metrics.md"
  log "visual metrics: $RUN_DIR/visual_metrics.md"
}


run_local_semantics() {
  local cache_dir="$HOME/.cache/video-script-analysic"
  local binary="$cache_dir/local-semantics"
  mkdir -p "$cache_dir"
  if [[ ! -x "$binary" ]]; then
    if [[ "$(uname -s)" == "Darwin" ]] && command -v swiftc >/dev/null 2>&1; then
      swiftc "$SCRIPT_DIR/local_semantics.swift" -o "$binary"
    else
      log "warning: local Vision semantics unavailable"
      return 0
    fi
  fi

  local frames
  frames=$(find "$RUN_DIR/frames" -maxdepth 1 -type f -name 's_*.jpg' | sort)
  if [[ -n "$frames" ]]; then
    # shellcheck disable=SC2086
    $binary $frames > "$RUN_DIR/local_semantics.json"
    log "local semantics: $RUN_DIR/local_semantics.json"
  fi
}

prepare_video_assets() {
  [[ -f "$RUN_DIR/video.mp4" ]] || return 0

  log "video: $RUN_DIR/video.mp4"
  duration="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$RUN_DIR/video.mp4" 2>/dev/null || echo 0)"
  if ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$RUN_DIR/video.mp4" | grep -q .; then
    ffmpeg -y -i "$RUN_DIR/video.mp4" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$RUN_DIR/audio.wav" -loglevel error
    transcribe_audio
  else
    log "warning: no audio stream found"
  fi

  rm -f "$RUN_DIR"/frames/*.jpg
  ffmpeg -i "$RUN_DIR/video.mp4" -vf "select='gt(scene,0.3)',showinfo" \
    -fps_mode vfr "$RUN_DIR/frames/cut_%03d.jpg" 2>"$RUN_DIR/cuts.log" || true
  ffmpeg -i "$RUN_DIR/video.mp4" -t 3 -vf "fps=1" \
    -fps_mode vfr "$RUN_DIR/frames/hook_%03d.jpg" -loglevel error || true

  # Sampling density: 1 fps for <=3m, 1 frame/3s for <=15m, then 1 frame/5s.
  sample_fps=1
  if awk -v d="$duration" 'BEGIN { exit !(d > 180 && d <= 900) }'; then
    sample_fps='1/3'
  elif awk -v d="$duration" 'BEGIN { exit !(d > 900) }'; then
    sample_fps='1/5'
  fi
  log "sampling frames: fps=$sample_fps"
  ffmpeg -i "$RUN_DIR/video.mp4" -vf "fps=$sample_fps" \
    -fps_mode vfr "$RUN_DIR/frames/s_%03d.jpg" -loglevel error || true

  if find "$RUN_DIR/frames" -type f -name 's_*.jpg' | grep -q .; then
    ffmpeg -pattern_type glob -i "$RUN_DIR/frames/s_*.jpg" \
      -filter_complex "scale=320:-1,tile=6x6:padding=4:color=white" \
      -fps_mode vfr "$RUN_DIR/sheet_%03d.jpg" -loglevel error || true
  fi

  run_visual_ocr
  run_visual_metrics
  run_local_semantics

  local duration cuts avg
  duration="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$RUN_DIR/video.mp4" 2>/dev/null || echo 0)"
  cuts="$(grep -c "pts_time" "$RUN_DIR/cuts.log" 2>/dev/null || echo 0)"
  avg="$(awk -v d="$duration" -v c="$cuts" 'BEGIN { if (c > 0) printf "%.2f", d/c; else printf "N/A" }')"
  cat > "$RUN_DIR/video_stats.txt" <<EOF
duration=$duration
cuts=$cuts
avg_cut=$avg
whisper_bin=$WHISPER_BIN_RESOLVED
whisper_model=$WHISPER_MODEL_RESOLVED
whisper_language=$WHISPER_LANGUAGE
ocr_evidence=$([[ -s "$RUN_DIR/ocr.md" ]] && echo yes || echo no)
visual_metrics=$([[ -s "$RUN_DIR/visual_metrics.md" ]] && echo yes || echo no)
EOF
}

prepare_image_sheet() {
  if ! find "$RUN_DIR/xhs_images" -type f | grep -q .; then
    return 0
  fi

  ffmpeg -pattern_type glob -i "$RUN_DIR/xhs_images/*" \
    -filter_complex "scale=360:-1,tile=4x4:padding=8:color=white" \
    -fps_mode vfr "$RUN_DIR/xhs_sheet_%03d.jpg" -loglevel error || true
}

download_xhs_assets_from_json() {
  local json_file="$1"
  local url_list="$RUN_DIR/xhs_download_urls.tsv"
  python3 - "$json_file" "$url_list" <<'PY'
import json, sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
payload = json.load(src.open(encoding="utf-8"))
data = payload.get("data") or {}
urls = data.get("下载地址") or []
kind = data.get("作品类型") or ""
rows = []
if "视频" in kind and urls:
    rows.append(("video", urls[0]))
else:
    for url in urls:
        rows.append(("image", url))
dst.write_text("\n".join(f"{kind}\t{url}" for kind, url in rows) + ("\n" if rows else ""), encoding="utf-8")
PY

  local i=0 kind url ext
  while IFS=$'\t' read -r kind url; do
    [[ -n "$url" ]] || continue
    case "$kind" in
      video)
        [[ -f "$RUN_DIR/video.mp4" ]] || curl -L -A "Mozilla/5.0" -H "Referer: https://www.xiaohongshu.com/" "$url" -o "$RUN_DIR/video.mp4"
        ;;
      image)
        i=$((i + 1))
        ext="${url%%\?*}"
        ext="${ext##*.}"
        case "$ext" in
          jpg|jpeg|png|webp|heic|avif) ;;
          *) ext="jpg" ;;
        esac
        curl -L -A "Mozilla/5.0" -H "Referer: https://www.xiaohongshu.com/" "$url" -o "$RUN_DIR/xhs_images/$(printf '%03d' "$i").$ext"
        ;;
    esac
  done < "$url_list"
}

srt_to_text() {
  local srt="$1"
  local out="$2"
  python3 - "$srt" "$out" <<'PY'
import re, sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
lines = []
for raw in src.read_text(encoding="utf-8", errors="ignore").splitlines():
    line = raw.strip()
    if not line:
        continue
    if line.isdigit():
        continue
    if re.match(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}", line):
        continue
    lines.append(line)

dst.write_text("\n".join(lines).strip() + ("\n" if lines else ""), encoding="utf-8")
PY
}

copy_new_xhs_files() {
  local marker="$1"
  if [[ ! -d "$XHS_DOWNLOAD_DIR" ]]; then
    log "warning: XHS_DOWNLOAD_DIR not found: $XHS_DOWNLOAD_DIR"
    log "set XHS_DOWNLOAD_DIR to the mounted XHS-Downloader Volume/Download directory to collect downloaded files"
    return 0
  fi

  local newest_video=""
  newest_video="$(find "$XHS_DOWNLOAD_DIR" -type f -newer "$marker" -iname '*.mp4' -print 2>/dev/null | sort | tail -1 || true)"
  if [[ -n "$newest_video" ]]; then
    cp "$newest_video" "$RUN_DIR/video.mp4"
  fi

  local i=0 ext base
  while IFS= read -r file; do
    i=$((i + 1))
    base="$(basename "$file")"
    ext="${base##*.}"
    cp "$file" "$RUN_DIR/xhs_images/$(printf '%03d' "$i").$ext"
  done < <(find "$XHS_DOWNLOAD_DIR" -type f -newer "$marker" \( \
      -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' -o -iname '*.heic' \
    \) -print 2>/dev/null | sort)
}

json_has_data() {
  python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        payload = json.load(f)
except Exception:
    raise SystemExit(1)
data = payload.get("data")
raise SystemExit(0 if data else 1)
PY
}

prepare_xhs_web_fallback() {
  local replace_metadata="${1:-true}"
  local page="$RUN_DIR/xhs_page.html"
  local web_meta="$RUN_DIR/xhs_web_meta.json"
  curl -sS -L -A "Mozilla/5.0" "$URL" -o "$page"

  python3 - "$page" "$web_meta" "$URL" <<'PY'
import json, re, sys
from pathlib import Path
from urllib.parse import urlparse

page = Path(sys.argv[1])
out = Path(sys.argv[2])
source_url = sys.argv[3]
s = page.read_text(encoding="utf-8", errors="ignore")
normalized = s.replace("\\u002F", "/")

def first(pattern, text=normalized, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(1) if m else ""

def json_string(key, text):
    value = first(rf'"{re.escape(key)}":"((?:\\.|[^"\\])*)"', text)
    if not value:
        return ""
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value

def json_number(key, text):
    return first(rf'"{re.escape(key)}":([0-9]+)', text)

path = urlparse(source_url).path
note_id = ""
for pattern in (
    r"/explore/([0-9a-f]+)",
    r"/discovery/item/([0-9a-f]+)",
    r"/user/profile/[a-z0-9]+/([0-9a-f]+)",
):
    note_id = first(pattern, path) or note_id
note_id = note_id or first(r'"firstNoteId":"([^"]+)"') or first(r'"noteId":"([^"]+)"')

note_marker = f'"noteDetailMap":{{"{note_id}"' if note_id else '"noteDetailMap":{'
note_pos = normalized.find(note_marker)
if note_pos < 0 and note_id:
    note_pos = normalized.find(f'"noteId":"{note_id}"')
segment = normalized[max(0, note_pos - 2000): note_pos + 50000] if note_pos >= 0 else normalized

video_urls = []
for pattern in (r'"master_url":"([^"]+)"', r'"masterUrl":"([^"]+)"'):
    video_urls.extend(re.findall(pattern, normalized))

subtitle_url = ""
subtitle_lang = ""
media_v2_raw = json_string("mediaV2", segment)
if media_v2_raw:
    try:
        media_v2 = json.loads(media_v2_raw)
        subtitles = (((media_v2.get("video") or {}).get("subtitles")) or {})
        for lang in ("source", "zh-CN", "en-US"):
            items = subtitles.get(lang) or []
            if items and items[0].get("url"):
                subtitle_url = items[0]["url"].replace("\\u002F", "/")
                subtitle_lang = items[0].get("language") or lang
                break
    except Exception:
        pass
if not subtitle_url:
    subtitle_url = first(r'https://sns-subtitle[^"\\]+', normalized)

meta = {
    "platform": "xhs",
    "type": first(r'"type":"([^"]+)"', segment),
    "noteId": note_id,
    "title": json_string("title", segment),
    "desc": json_string("desc", segment),
    "author": json_string("nickname", segment),
    "time": json_number("time", segment),
    "lastUpdateTime": json_number("lastUpdateTime", segment),
    "likedCount": first(r'"likedCount":"([^"]+)"', segment),
    "collectedCount": first(r'"collectedCount":"([^"]+)"', segment),
    "commentCount": first(r'"commentCount":"([^"]+)"', segment),
    "shareCount": first(r'"shareCount":"([^"]+)"', segment),
    "videoUrl": video_urls[0] if video_urls else "",
    "subtitleUrl": subtitle_url,
    "subtitleLanguage": subtitle_lang,
    "fallback": "xhs_web_html",
}

out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(meta, ensure_ascii=False))
PY

  local video_url subtitle_url
  video_url="$(python3 - "$web_meta" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("videoUrl", ""))
PY
)"
  subtitle_url="$(python3 - "$web_meta" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("subtitleUrl", ""))
PY
)"

  if [[ -n "$video_url" && ! -f "$RUN_DIR/video.mp4" ]]; then
    curl -L -A "Mozilla/5.0" -H "Referer: https://www.xiaohongshu.com/" "$video_url" -o "$RUN_DIR/video.mp4"
  fi
  if [[ -n "$subtitle_url" ]]; then
    curl -L -A "Mozilla/5.0" -H "Referer: https://www.xiaohongshu.com/" "$subtitle_url" -o "$RUN_DIR/subtitle.srt" || true
    if [[ -s "$RUN_DIR/subtitle.srt" ]]; then
      srt_to_text "$RUN_DIR/subtitle.srt" "$RUN_DIR/audio.txt"
    fi
  fi
  if [[ "$replace_metadata" == "true" ]]; then
    cp "$web_meta" "$METADATA"
  fi
}

prepare_xhs_detail_retry() {
  local detail="$RUN_DIR/xhs_detail_retry.json"
  curl -sS -X POST "$XHS_API_BASE/xhs/detail" \
    -H "Content-Type: application/json" \
    -d "$(json_payload false)" \
    -o "$detail"

  if ! json_has_data "$detail"; then
    return 1
  fi

  cp "$detail" "$METADATA"
  download_xhs_assets_from_json "$detail"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$INPUT" ]]; then
  PLATFORM="local-file"
  ABS_VIDEO="$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")"
else
  PLATFORM="$(detect_platform)"
fi

: > "$MANIFEST"
printf '%s\n' "$INPUT" > "$RUN_DIR/source.url"
log "run_dir: $RUN_DIR"
log "platform: $PLATFORM"
log "input: $INPUT"

if [[ "$PLATFORM" == "local-file" ]]; then
  cp "$ABS_VIDEO" "$RUN_DIR/video.mp4"
  ffprobe -v quiet -print_format json -show_format -show_streams "$ABS_VIDEO" > "$RUN_DIR/ffprobe.json"
  python3 - "$RUN_DIR/ffprobe.json" "$METADATA" "$ABS_VIDEO" <<'PY'
import json, sys
from pathlib import Path
probe = json.load(open(sys.argv[1], encoding='utf-8'))
source = Path(sys.argv[3])
fmt = probe.get('format', {})
streams = probe.get('streams', [])
video = next((x for x in streams if x.get('codec_type') == 'video'), {})
audio = next((x for x in streams if x.get('codec_type') == 'audio'), {})
meta = {
    'platform': 'local-file',
    'type': 'video',
    'source_file': str(source),
    'file_name': source.name,
    'duration': float(fmt.get('duration') or 0),
    'size_bytes': int(fmt.get('size') or source.stat().st_size),
    'width': int(video.get('width') or 0),
    'height': int(video.get('height') or 0),
    'video_codec': video.get('codec_name'),
    'audio_codec': audio.get('codec_name'),
    'message': 'Local file input; platform metadata unavailable unless a source URL is provided.',
}
json.dump(meta, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
PY
else
  URL="$INPUT"
  case "$PLATFORM" in
    xhs)
      marker="$RUN_DIR/download.marker"
      : > "$marker"
      curl -sS -X POST "$XHS_API_BASE/xhs/detail" \
        -H "Content-Type: application/json" \
        -d "$(json_payload false)" \
        -o "$METADATA"
      if json_has_data "$METADATA"; then
        download_xhs_assets_from_json "$METADATA"
        prepare_xhs_web_fallback false
      else
        warn_xhs_cookie_if_needed
        log "warning: XHS API detail-only returned no data; retrying download=true mode"
        curl -sS -X POST "$XHS_API_BASE/xhs/detail" \
          -H "Content-Type: application/json" \
          -d "$(json_payload true)" \
          -o "$METADATA"
        copy_new_xhs_files "$marker"
        if ! json_has_data "$METADATA"; then
          log "warning: XHS API download=true returned no data; trying web HTML fallback"
          prepare_xhs_web_fallback
        elif [[ ! -f "$RUN_DIR/video.mp4" ]] && ! find "$RUN_DIR/xhs_images" -type f | grep -q .; then
          download_xhs_assets_from_json "$METADATA"
          prepare_xhs_web_fallback false
        else
          prepare_xhs_web_fallback false
        fi
      fi
      ;;
    douyin|tiktok|bilibili)
      encoded_url="$(python3 - "$URL" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=""))
PY
)"
      curl -sS "$DOUYIN_API_BASE/api/hybrid/video_data?url=$encoded_url&minimal=false" -o "$METADATA"
      if video_url="$(extract_video_url)"; then
        case "$PLATFORM" in
          douyin) referer="https://www.douyin.com/" ;;
          tiktok) referer="https://www.tiktok.com/" ;;
          bilibili) referer="https://www.bilibili.com/" ;;
        esac
        download_video_from_url "$video_url" "$referer"
      else
        log "warning: no video url found in metadata"
      fi
      ;;
    *)
      log "warning: platform not automated by this script; use TikHub/Jina/browser fallback"
      printf '{"url": %s, "platform": %s, "message": "manual fallback required"}\n' \
        "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$URL")" \
        "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$PLATFORM")" > "$METADATA"
      ;;
  esac
fi

prepare_video_assets
prepare_image_sheet

{
  echo ""
  echo "outputs:"
  find "$RUN_DIR" -maxdepth 2 -type f | sort
} >> "$MANIFEST"

log "verifying evidence package"
python3 "$SCRIPT_DIR/verify_evidence.py" "$RUN_DIR"
log "done: $RUN_DIR"
