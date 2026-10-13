#!/usr/bin/env python3
"""Verify that prepare-assets produced a complete benchmark evidence package."""
import json
import re
import subprocess
import sys
from pathlib import Path


def fail(message, failures):
    failures.append(message)
    print(f"✗ {message}")


def warn(message):
    print(f"! {message}")


def ok(message):
    print(f"✓ {message}")


def srt_end_seconds(path: Path):
    if not path.is_file():
        return None
    matches = re.findall(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", path.read_text(encoding="utf-8", errors="ignore"))
    if not matches:
        return None
    _, _, _, _, h, m, s, ms = matches[-1]
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def main(run_dir: str):
    root = Path(run_dir)
    failures = []
    if not root.is_dir():
        print(f"✗ run directory not found: {root}", file=sys.stderr)
        return 1

    for name in ("metadata.json", "video.mp4"):
        path = root / name
        if path.is_file() and path.stat().st_size > 0:
            ok(name)
        else:
            fail(f"missing or empty {name}", failures)

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(root / "video.mp4")],
            text=True, capture_output=True, check=True,
        )
        data = json.loads(probe.stdout)
    except Exception as exc:
        fail(f"ffprobe could not read video.mp4: {exc}", failures)
        data = {"streams": [], "format": {}}

    streams = data.get("streams", [])
    duration = float(data.get("format", {}).get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    if has_audio:
        for name in ("audio.wav", "audio.srt", "audio.txt"):
            path = root / name
            if path.is_file() and path.stat().st_size > 0:
                ok(name)
            else:
                fail(f"missing or empty {name}", failures)
        end = srt_end_seconds(root / "audio.srt")
        if end is not None:
            if end <= duration + 2:
                ok(f"transcript ends at {end:.1f}s; video is {duration:.1f}s")
            else:
                fail(f"transcript ends at {end:.1f}s but video is only {duration:.1f}s (likely silence hallucination)", failures)
    else:
        warn("video has no audio stream; transcript intentionally absent")

    sample_frames = sorted(root.glob("frames/s_*.jpg"))
    expected_min = max(1, int(duration // 5)) if duration else 1
    if sample_frames:
        ok(f"{len(sample_frames)} one-interval sample frames")
    else:
        fail("no frames/s_*.jpg sample frames", failures)
    if duration and len(sample_frames) < expected_min:
        warn(f"only {len(sample_frames)} frames for {duration:.1f}s; expected roughly at least {expected_min}")

    sheets = sorted(root.glob("sheet_*.jpg"))
    if sheets:
        ok(f"{len(sheets)} contact sheet(s)")
    else:
        fail("no sheet_*.jpg contact sheet", failures)

    ocr = root / "ocr.md"
    if ocr.is_file() and ocr.stat().st_size > 0:
        ok("screen-text OCR evidence")
    else:
        warn("ocr.md empty or absent: acceptable if the video has no on-screen text or Vision OCR is unavailable")

    metrics = root / "visual_metrics.md"
    if metrics.is_file() and metrics.stat().st_size > 0:
        ok("visual metrics evidence")
    else:
        warn("visual_metrics.md absent; PIL was unavailable or analysis failed")

    stats = root / "video_stats.txt"
    if stats.is_file():
        model_line = next((line for line in stats.read_text(errors="ignore").splitlines() if line.startswith("whisper_model=")), "")
        model = model_line.split("=", 1)[1] if model_line else ""
        if model in ("tiny", "base", "small"):
            warn(f"Whisper model is {model}; suitable for fast regression but verify terminology on important benchmarks")
    else:
        warn("video_stats.txt absent")

    if failures:
        print(f"\n✗ evidence verification failed: {len(failures)} issue(s)")
        return 1
    print("\n✓ evidence package is complete")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_evidence.py <run_dir>")
    raise SystemExit(main(sys.argv[1]))
