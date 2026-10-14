#!/usr/bin/env python3
"""Build a Seedance-ready prompt from a local product-video evidence package.

Local/deterministic only: this script does not call cloud LLM/VLM services.
It combines ffprobe metadata, local Whisper SRT, macOS Vision OCR/semantics,
scene cuts, sampled frames, PIL metrics and an optional user observations JSON.

The observations file is intentionally supported because local Vision cannot
reliably infer fine facts such as age, ethnicity, lip piercing, shirt graphics,
exact room props, product claims or adapted screen copy.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
RAW_OCR_RE = re.compile(r"^\[confidence=([0-9.]+)\s+y=([0-9.]+)\s+x=([0-9.]+)\]\s*(.+?)\s*$")
RAW_OCR_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")

OVERLAY_WORDS = {"STOP", "NEW", "HOT", "SALE", "OFFER", "SHOP", "BUY"}
PRICE_RE = re.compile(r"[£$¥€%]|(?:\d+(?:\.\d+)?\s*(?:quid|pounds?|dollars?|usd|gbp|eur|euros?|rmb|yuan|元|镑|折|%))", re.I)
PERSON_LABELS = {"people", "adult", "person", "man", "woman", "boy", "girl", "teen", "child"}
PRODUCT_LABELS = {
    "tool", "cosmetic_tool", "consumer_electronics", "machine", "container",
    "carton", "bottle", "jar", "can", "box", "package", "electronic_device",
}
DECOR_LABELS = {"art", "decoration", "frame", "painting", "picture_frame"}


def srt_time_to_seconds(value: str) -> float:
    m = TIME_RE.search(value)
    if not m:
        return 0.0
    h, minute, sec, ms = map(int, m.groups())
    return h * 3600 + minute * 60 + sec + ms / 1000


def load_json(path: Path | str, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def parse_srt(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8", errors="ignore").strip()
    chunks = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        timing = next((x for x in lines if "-->" in x), None)
        if not timing:
            continue
        a, b = timing.split("-->")
        text = " ".join(x for x in lines if "-->" not in x and not x.isdigit()).strip()
        chunks.append({"start": srt_time_to_seconds(a), "end": srt_time_to_seconds(b), "text": text})
    return chunks


def frame_time_from_name(name: str) -> int | None:
    m = re.search(r"s_(\d+)\.jpe?g$", name)
    return int(m.group(1)) - 1 if m else None


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def meaningful_ocr(text: str) -> bool:
    text = normalize_text(text)
    if not text:
        return False
    if text.upper() in OVERLAY_WORDS:
        return True
    if PRICE_RE.search(text):
        return True
    return len(text) >= 3


def parse_raw_ocr(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    items = []
    current = None

    def flush():
        nonlocal current
        if current and current["lines"]:
            items.append(current)
        current = None

    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        heading = RAW_OCR_HEADING_RE.match(raw.strip())
        if heading:
            flush()
            filename = heading.group(1)
            t = frame_time_from_name(filename)
            current = {"time": 0 if t is None else t, "file": filename, "lines": []}
            continue
        if not current:
            continue
        m = RAW_OCR_RE.match(raw.strip())
        if not m:
            continue
        confidence, y, x, text = m.groups()
        text = normalize_text(text)
        if meaningful_ocr(text):
            current["lines"].append({
                "text": text,
                "confidence": float(confidence),
                "y": float(y),
                "x": float(x),
            })
    flush()
    return items


def parse_cut_times(path: Path) -> list[float]:
    if not path.is_file():
        return []
    times = []
    for m in re.finditer(r"pts_time:([0-9.]+)", path.read_text(encoding="utf-8", errors="ignore")):
        try:
            value = float(m.group(1))
        except ValueError:
            continue
        if not times or abs(value - times[-1]) > 0.2:
            times.append(value)
    return times


def collect_frames(frames_dir: Path) -> list[dict]:
    frames = []
    for path in sorted(frames_dir.glob("s_*.jpg")):
        t = frame_time_from_name(path.name)
        if t is not None:
            frames.append({"time": t, "path": path})
    return frames


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def nearest_frame(frames: list[dict], target: float) -> dict | None:
    return min(frames, key=lambda x: abs(x["time"] - target)) if frames else None


def representative_frames(frames: list[dict], start: float, end: float, limit: int = 3) -> list[dict]:
    targets = [start, (start + end) / 2, max(start, end - 0.2)]
    result, seen = [], set()
    for target in targets:
        frame = nearest_frame(frames, target)
        if frame and str(frame["path"]) not in seen:
            seen.add(str(frame["path"]))
            result.append(frame)
    return result[:limit]


def merge_short_intervals(boundaries: list[float], min_duration: float = 3.0, max_merged: float = 11.5) -> list[float]:
    bounds = boundaries[:]
    for _ in range(30):
        intervals = list(zip(bounds, bounds[1:]))
        if not intervals:
            break
        idx = min(range(len(intervals)), key=lambda i: intervals[i][1] - intervals[i][0])
        start, end = intervals[idx]
        if end - start >= min_duration:
            break
        candidates = []
        if idx > 0:
            candidates.append((idx - 1, start - intervals[idx - 1][0]))
        if idx + 1 < len(intervals):
            candidates.append((idx + 1, intervals[idx + 1][1] - start))
        candidates = [(i, d) for i, d in candidates if d <= max_merged]
        if not candidates:
            break
        neighbor = min(candidates, key=lambda item: item[1])[0]
        # Shared boundary is idx+1 when merging right, idx when merging left.
        remove_boundary = idx + 1 if neighbor == idx + 1 else idx
        del bounds[remove_boundary]
    return bounds


def split_long_intervals(boundaries: list[float], transcript: list[dict], max_duration: float = 10.0) -> list[float]:
    bounds = boundaries[:]
    for _ in range(30):
        intervals = list(zip(bounds, bounds[1:]))
        long_idx = next((i for i, (a, b) in enumerate(intervals) if b - a > max_duration), None)
        if long_idx is None:
            break
        a, b = intervals[long_idx]
        midpoint = (a + b) / 2
        candidates = []
        for chunk in transcript:
            t = chunk["start"]
            if a + 1.5 < t < b - 1.5:
                candidates.append((abs(t - midpoint), t))
        split_at = min(candidates, key=lambda item: item[0])[1] if candidates else midpoint
        bounds.insert(long_idx + 1, split_at)
        bounds.sort()
    return bounds


def build_segments(duration: float, cuts: list[float], frames: list[dict], transcript: list[dict]) -> list[dict]:
    initial = [0.0]
    for t in cuts:
        if 0.5 < t < duration - 0.5 and all(abs(t - x) > 0.2 for x in initial):
            initial.append(t)
    initial.append(duration)
    bounds = merge_short_intervals(initial)
    bounds = split_long_intervals(bounds, transcript)

    segments = []
    for i, (start, end) in enumerate(zip(bounds, bounds[1:]), 1):
        segments.append({
            "index": i,
            "start": start,
            "end": end,
            "frames": representative_frames(frames, start, end),
            "spoken": [],
            "ocr": [],
            "labels": [],
            "label_counts": {},
            "face_count": 0,
            "human_count": 0,
            "people_label_frames": 0,
            "decor_label_frames": 0,
            "product_label_frames": 0,
            "max_face_width": 0.0,
        })

    for chunk in transcript:
        best, best_overlap = None, 0
        for seg in segments:
            value = overlap(chunk["start"], chunk["end"], seg["start"], seg["end"])
            if value > best_overlap:
                best, best_overlap = seg, value
        if best is not None:
            best["spoken"].append(chunk)
    return segments


def is_product_surface_text(text: str, confidence: float, y: float, aliases: set[str]) -> bool:
    norm = normalize_text(text)
    compact = re.sub(r"[^a-z0-9]+", "", norm.lower())
    alias_values = set()
    for alias in aliases:
        alias = normalize_text(str(alias))
        if alias:
            alias_values.add(alias.lower())
            alias_values.add(re.sub(r"[^a-z0-9]+", "", alias.lower()))
    if compact in alias_values or norm.lower() in alias_values:
        return True
    if compact in {"oneblade", "oneblade360"}:
        return True
    if re.fullmatch(r"ph(?:il|o)[a-z]+", compact or "") and confidence >= 0.25:
        return True
    # Product/packaging logo in the reference video. Do not turn this into a
    # generated overlay; the user product's own logo should come from its image.
    if compact == "blade" and confidence >= 0.45 and y < 0.65:
        return True
    if compact == "36" and confidence >= 0.9:
        return True
    return False


def attach_visual_evidence(segments: list[dict], semantics: dict, ocr_items: list[dict], aliases: set[str]):
    by_time = {int(x.get("time", -1)): x for x in semantics.get("frames", [])}
    for seg in segments:
        label_counts: dict[str, float] = {}
        faces = humans = people_frames = decor_frames = product_frames = 0
        max_face_width = 0.0

        for t in range(int(math.floor(seg["start"])), int(math.ceil(seg["end"])) + 1):
            frame = by_time.get(t)
            if not frame:
                continue
            faces += int(frame.get("faceCount", 0) or 0)
            humans += int(frame.get("humanCount", 0) or 0)
            frame_has_person = False
            frame_has_decor = False
            frame_has_product = False
            for box in frame.get("faceBoxes", []):
                if len(box) >= 3:
                    max_face_width = max(max_face_width, min(1.0, float(box[2])))
            for raw_label in frame.get("labels", [])[:12]:
                name, _, conf = raw_label.partition(":")
                try:
                    confidence = float(conf)
                except ValueError:
                    confidence = 0.0
                name = name.strip()
                if not name:
                    continue
                label_counts[name] = max(label_counts.get(name, 0.0), confidence)
                if name in PERSON_LABELS and confidence >= 0.45:
                    frame_has_person = True
                if name in DECOR_LABELS and confidence >= 0.25:
                    frame_has_decor = True
                if name in PRODUCT_LABELS and confidence >= 0.08:
                    frame_has_product = True
            people_frames += int(frame_has_person)
            decor_frames += int(frame_has_decor)
            product_frames += int(frame_has_product)

        seg["labels"] = [name for name, _ in sorted(label_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]]
        seg["label_counts"] = label_counts
        seg["face_count"] = faces
        seg["human_count"] = humans
        seg["people_label_frames"] = people_frames
        seg["decor_label_frames"] = decor_frames
        seg["product_label_frames"] = product_frames
        seg["max_face_width"] = max_face_width

        grouped: dict[str, dict] = {}
        for item in ocr_items:
            if seg["start"] - 0.5 <= item["time"] <= seg["end"] + 0.5:
                for line in item["lines"]:
                    key = normalize_text(line["text"].lower())
                    product_surface = is_product_surface_text(line["text"], line["confidence"], line["y"], aliases)
                    if key not in grouped:
                        grouped[key] = {**line, "count": 0, "product_surface": product_surface}
                    grouped[key]["count"] += 1
                    grouped[key]["product_surface"] = grouped[key]["product_surface"] or product_surface
        evidence = []
        for value in grouped.values():
            high = value["confidence"] >= 0.75
            repeated = value["count"] >= 2 and value["confidence"] >= 0.25
            if high or repeated:
                evidence.append(value)
        evidence.sort(key=lambda x: (x["confidence"], x["count"]), reverse=True)
        seg["ocr"] = evidence[:10]


def aspect_label(width: int, height: int) -> str:
    if not width or not height:
        return "沿用原片比例"
    ratio = width / height
    for label, candidate in [("9:16", 9 / 16), ("16:9", 16 / 9), ("3:4", 3 / 4), ("1:1", 1)]:
        if math.isclose(ratio, candidate, abs_tol=0.03):
            return label
    return f"{width}:{height}"


def metric_avg(metrics: list[dict], key: str) -> float | None:
    values = [float(x[key]) for x in metrics if isinstance(x.get(key), (int, float))]
    return sum(values) / len(values) if values else None


def brightness_word(value: float | None) -> str:
    if value is None:
        return "曝光参考原帧"
    if value >= 155:
        return "明亮高调"
    if value >= 115:
        return "自然中等亮度"
    return "偏低亮度但保持商品清晰"


def saturation_word(value: float | None) -> str:
    if value is None:
        return "色彩浓度参考原帧"
    if value >= 55:
        return "饱和度偏高"
    if value >= 30:
        return "自然中等饱和"
    return "低饱和/克制色彩"


def contrast_word(value: float | None) -> str:
    if value is None:
        return "反差参考原帧"
    if value >= 75:
        return "对比偏强"
    if value >= 55:
        return "对比自然"
    return "对比柔和"


def joined_text(seg: dict) -> str:
    return " ".join(x["text"].lower() for x in seg.get("spoken", []))


def normalize_presence_mode(value) -> str:
    """Normalize user-provided presence mode.

    Local Vision labels can confuse products/backgrounds with people, so
    observations.json can force person / hands-only / none.
    """
    text = normalize_text(str(value or "")).lower()
    if not text or text in {"auto", "自动", "自动判断"}:
        return "auto"
    if any(token in text for token in ("hand", "hands", "手", "只露手", "手部")):
        return "hands"
    if any(token in text for token in ("none", "no person", "no people", "无人", "无人物", "没有人")):
        return "none"
    if any(token in text for token in ("person", "people", "present", "真人", "有人", "人物", "出镜")):
        return "person"
    return "auto"


def shot_at_time(observations: dict, midpoint: float) -> dict:
    for shot in observations.get("shots", []) or []:
        try:
            start = float(shot.get("start", -1))
            end = float(shot.get("end", 999999))
        except Exception:
            continue
        if start <= midpoint < end:
            return shot
    return {}


def apply_presence_override(segments: list[dict], observations: dict):
    global_data = observations.get("global", {}) if isinstance(observations.get("global"), dict) else {}
    global_mode = normalize_presence_mode(
        global_data.get("person_mode")
        or global_data.get("presence_mode")
        or global_data.get("people_mode")
        or global_data.get("subject_mode")
    )
    for seg in segments:
        shot = shot_at_time(observations, (seg["start"] + seg["end"]) / 2)
        mode = normalize_presence_mode(
            shot.get("person_mode")
            or shot.get("presence_mode")
            or shot.get("people_mode")
        )
        if mode == "auto":
            mode = global_mode
        seg["presence_mode"] = mode
        seg["hands_only"] = mode == "hands"
        if mode == "person":
            seg["person_override"] = True
        elif mode in {"hands", "none"}:
            seg["person_override"] = False
        else:
            seg["person_override"] = None


def person_present(seg: dict) -> bool:
    if seg.get("person_override") is not None:
        return bool(seg["person_override"])
    return bool(seg["face_count"] or seg["human_count"] or seg["people_label_frames"])


def hands_only(seg: dict) -> bool:
    return bool(seg.get("hands_only"))


def infer_kind(segments: list[dict], transcript: list[dict], presence_mode: str = "auto") -> str:
    spoken = sum(len(x["text"]) for x in transcript)
    person_segments = sum(1 for seg in segments if person_present(seg))
    hands_segments = sum(1 for seg in segments if hands_only(seg))
    if not segments:
        return "商品展示型"
    if presence_mode == "none" or (presence_mode == "auto" and person_segments == 0 and hands_segments == 0):
        return "商品/场景展示型（无人出镜）"
    if presence_mode == "hands" or (presence_mode == "auto" and person_segments == 0 and hands_segments):
        return "手部操作/商品展示型（只露手）"
    if person_segments >= len(segments) * 0.45 and spoken > 20:
        return "真人出镜讲解/演示型"
    if person_segments and spoken == 0:
        return "人物动作/场景种草型"
    return "商品展示混合型"


def shot_size(seg: dict, purpose: str = "") -> str:
    if "配件" in purpose or "套装" in purpose:
        return "人物中近景穿插配件/商品特写"
    if "商品出场" in purpose:
        return "人物中近景转商品整体近景"
    if "功能证明" in purpose or "使用效果" in purpose:
        return "人物使用中近景穿插接触面/商品细节特写"
    if "价格" in purpose or "CTA" in purpose:
        return "人物中近景，商品与指向手势保持清晰"
    if seg["max_face_width"] >= 0.55:
        return "面部大近景/特写"
    if seg["max_face_width"] >= 0.35:
        return "面部或胸部以上中近景"
    labels = set(seg["labels"])
    if labels & PRODUCT_LABELS or seg["product_label_frames"]:
        return "商品/道具特写"
    if person_present(seg):
        return "人物中景或动作镜头"
    return "商品、手部或环境镜头"


def product_intro_time(transcript: list[dict], observations: dict) -> float | None:
    obs = observations.get("product", {}).get("intro_time")
    if isinstance(obs, (int, float)):
        return float(obs)
    patterns = [
        r"\bi now use\b", r"\bi'?ve switched\b", r"\bi switched\b", r"\bswitch(?:ed)? to\b",
        r"\bmeet\b", r"\bintroducing\b", r"\bthis is\b", r"\bhere(?:'s| is)\b",
        r"现在用", r"换用", r"改用", r"这是", r"这款", r"入手",
    ]
    for chunk in transcript:
        text = chunk["text"].lower()
        if any(re.search(pattern, text) for pattern in patterns):
            return float(chunk["start"])
    return None


def infer_purpose(seg: dict, intro_time: float | None) -> str:
    text = joined_text(seg)
    overlay_text = " ".join(x["text"].lower() for x in seg["ocr"] if not x.get("product_surface"))
    evidence = f"{text} {overlay_text}"
    if seg["start"] < 4.0 and (
        "stop" in evidence
        or re.search(r"wrong|problem|hurt|rash|don'?t|never|stop|pain|mistake|痛|错|别再|不要|坑|过敏", evidence)
    ):
        return "开场钩子：用问题、错误做法或反差抓住目标用户"
    if re.search(r"\b(link|sale|price|discount|grab|buy|shop|offer|checkout|order|limited|£|\$|¥|€|%)\b|优惠|折扣|链接|下单|购买|价格|限时|库存", evidence):
        return "价格/优惠/CTA：引导点击、下单或进一步了解"
    if intro_time is not None and seg["end"] <= intro_time + 0.2:
        return "旧方案/需求铺垫：展示商品出现前的问题或使用场景"
    if re.search(r"shower|waterproof|water|easy|clean|防水|水洗|清洁|方便", evidence):
        return "功能证明：把核心卖点转成可见操作"
    if re.search(r"close\s+to\s+(?:the\s+)?skin|contour|foam|rash|ingrown|cuts?\s*,?\s*nicks?|scrapes?|skin[ -]?safe|贴合|轮廓|泡沫|泛红|刮伤|皮肤", evidence):
        return "使用效果证明：展示商品如何贴合、保护皮肤或完成核心使用动作"
    if re.search(r"month|lasts?|durable|battery|续航|耐用|持久|省电", evidence):
        return "耐用/使用成本证明"
    if re.search(r"spare|comb|guard|accessor|comes with|include|set|kit|piece|配件|套装|赠品|备用|组件", evidence):
        return "配件/套装价值：展示内容物和性价比"
    if intro_time is not None and seg["start"] <= intro_time <= seg["end"]:
        return "商品出场：第一次清晰展示用户商品"
    if person_present(seg):
        return "人物演示：用真实使用动作展示商品利益"
    return "商品细节/场景展示：呈现外观、材质、使用环境或效果"


def visual_action_fallback(seg: dict, purpose: str, intro_time: float | None) -> str:
    if "开场钩子" in purpose:
        return "按参考帧完成开场动作：人物或手部直接展示错误做法、问题状态或核心使用场景；动作停顿半秒让观众看懂冲突，视线和表情对镜头，不额外增加参考帧里没有的道具。"
    if "旧方案" in purpose:
        return "展示商品出现前的旧工具、旧状态或真实需求；把问题位置靠近镜头，旧道具去品牌化，画面重点和动作方向严格跟随参考帧。"
    if "商品出场" in purpose:
        return "自然完成从旧状态到用户商品的切换：商品从参考帧中的位置进入画面，先给整体轮廓，再给接触/使用部位的清晰近景，握姿和角度跟随参考帧。"
    if "功能证明" in purpose:
        return "把口播提到的功能变成可见操作：人物或手部按参考帧启动、接触、移动、清洁或展示商品；没有出现在参考帧或用户资料里的功能效果不要生成。"
    if "耐用" in purpose:
        return "用参考帧中的握持、反复使用、材质特写或替换动作表达耐用/使用成本；不自行生成数字、对比实验或损坏效果。"
    if "配件" in purpose:
        return "依次展示参考帧中实际出现且用户商品确实包含的组件；每个配件短暂停顿，数量、形状和组合以用户商品图为准，原片有但用户商品没有的配件直接删除。"
    if "价格" in purpose or "CTA" in purpose:
        return "人物或镜头继续展示商品，结尾按参考帧的位置做出指向链接/购物入口/左下角的动作；价格、折扣和倒计时只使用用户提供的成片文字，没有资料则只做指向动作。"
    if person_present(seg):
        return "人物按参考帧自然操作或展示商品，保持真实 UGC 反应；手部移动方向、身体角度、视线停顿、商品与脸/身体的位置关系与原片一致。"
    return "镜头围绕商品或使用场景做参考帧中的展示动作，保持商品主体清晰，呈现外观、材质、细节和使用环境；不新增人物、夸张运镜或影棚灯光。"


def obs_shot(seg: dict, observations: dict) -> dict:
    return shot_at_time(observations, (seg["start"] + seg["end"]) / 2)


def obs_global(observations: dict, key: str, fallback: str = "") -> str:
    return str(observations.get("global", {}).get(key) or fallback)


def screen_texts_for_shot(seg: dict, shot_obs: dict) -> tuple[list[str], list[dict], list[dict]]:
    overlay_ocr = [x for x in seg["ocr"] if not x.get("product_surface")]
    product_ocr = [x for x in seg["ocr"] if x.get("product_surface")]
    if shot_obs.get("screen_text") is not None:
        value = shot_obs.get("screen_text")
        if isinstance(value, list):
            return [str(x) for x in value if str(x).strip()], overlay_ocr, product_ocr
        if str(value).strip():
            return [str(value).strip()], overlay_ocr, product_ocr
        return [], overlay_ocr, product_ocr

    texts, stop_phrase, saw_stop = [], "", False
    for item in overlay_ocr:
        norm = normalize_text(item["text"])
        upper = norm.upper()
        compact = re.sub(r"[^a-z0-9]+", " ", norm.lower()).strip()
        if upper == "STOP" or upper.startswith("STOP "):
            saw_stop = True
        # Handles both "STOP shaving upward" and OCR variants such as
        # "STOP X shaving like this" (X is often the cross icon).
        m = re.search(r"shav\w*(?:\s+[a-z])?\s+(?:like\s+this|up|upward)", compact)
        if m:
            stop_phrase = m.group(0)
            saw_stop = True
        elif PRICE_RE.search(norm):
            texts.append("[价格/优惠信息，以用户提供文案为准]")
        elif upper != "STOP" and item["confidence"] >= 0.75 and (item["y"] >= 0.65 or item["y"] <= 0.08) and len(norm) >= 4:
            texts.append(f"【原片字幕：{norm}；需用户确认后使用】")
    if saw_stop:
        if stop_phrase:
            texts.insert(0, f"STOP {stop_phrase}（按用户商品改写错误动作；版式参考原帧）")
        else:
            texts.insert(0, "STOP【用户商品对应的错误动作/禁忌用法】（版式参考原帧）")
    dedup = []
    for text in texts:
        if text not in dedup:
            dedup.append(text)
    return dedup, overlay_ocr, product_ocr


def product_phase_note(seg: dict, intro_time: float | None) -> str:
    if intro_time is None:
        return "商品出现时机严格参考对应原帧；用户商品只在原片商品/主体商品应出现的位置出现。"
    if seg["end"] < intro_time:
        return "本段处于商品出场前：只表现无品牌旧工具或前置状态，不出现用户商品，不出现竞品清晰 logo。"
    if seg["start"] <= intro_time <= seg["end"]:
        return "本段完成新旧切换：前半段延续前置状态，后半段让用户商品第一次清晰进入画面。"
    return "本段使用用户商品：商品必须清晰可辨，外观严格绑定用户商品参考图。"


def brand_aliases(observations: dict, cli_aliases: str, ocr_items: list[dict]) -> set[str]:
    aliases = {"oneblade", "one blade"}
    raw = observations.get("product", {}).get("brand_aliases") or observations.get("product", {}).get("aliases") or []
    if isinstance(raw, str):
        raw = re.split(r"[,，;；|]", raw)
    if isinstance(raw, list):
        aliases.update(str(x).strip().lower() for x in raw if str(x).strip())
    if cli_aliases:
        aliases.update(x.strip().lower() for x in re.split(r"[,，;；|]", cli_aliases) if x.strip())
    for item in ocr_items:
        for line in item["lines"]:
            compact = re.sub(r"[^a-z0-9]+", "", line["text"].lower())
            if compact in {"oneblade", "philips", "philds"}:
                aliases.add(compact)
                aliases.add(line["text"].lower())
    return {x for x in aliases if x}


def mask_voiceover(text: str, aliases: set[str]) -> str:
    masked = text
    # Long product phrases first, so generic alias replacement does not create
    # awkward text such as "the [商品名]".
    masked = re.sub(r"\bthe\s+one\s+blade\b|\btheir\s+one\s+blade\b", "[商品名]", masked, flags=re.I)
    phrases = sorted(aliases - {"one blade", "oneblade"}, key=len, reverse=True)
    for alias in phrases:
        if not alias:
            continue
        masked = re.sub(rf"(?<!\w){re.escape(alias)}(?!\w)", "[商品名/品牌名，以用户资料为准]", masked, flags=re.I)
    for alias in ("oneblade", "philips", "philds"):
        masked = re.sub(rf"(?<!\w){re.escape(alias)}(?!\w)", "[商品名/品牌名，以用户资料为准]", masked, flags=re.I)
    masked = re.sub(r"\b([A-Z][a-z]+)\s+(?=have got\b)", "[品牌名] ", masked)

    # Time-sensitive/commercial facts that must not be copied to another product.
    masked = re.sub(r"\b\d+(?:\.\d+)?\s*(?:%|percent)\b", "[优惠力度，以用户资料为准]", masked, flags=re.I)
    masked = re.sub(r"[£$€¥]\s?\d+(?:\.\d+)?", "[价格，以用户资料为准]", masked)
    masked = re.sub(r"\b\d+(?:\.\d+)?\s*(?:quid|pounds?|dollars?|usd|gbp|eur|euros?|rmb|yuan|元|镑)\b", "[价格，以用户资料为准]", masked, flags=re.I)
    masked = re.sub(r"\b\d+\s*in\s*(?:one|1)\b|\bfive[ -]in[ -]one\b|\b5[ -]in[ -]1\b", "[档位/规格，以用户资料为准]", masked, flags=re.I)
    masked = re.sub(r"\b(?:\d+|[a-z]+)\s+months?\b", "[使用周期，以用户资料为准]", masked, flags=re.I)
    masked = re.sub(r"\b\d+\s*technology\b|\b360\s+technology\b", "[技术名称，以用户资料为准]", masked, flags=re.I)
    masked = re.sub(r"\b(?:completely\s+)?(?:shower[ -]?proof|waterproof)\b", "[防水等级，以用户资料为准]", masked, flags=re.I)
    masked = re.sub(r"\b(?:two|three|\d+)\s+(?:body\s+)?(?:combs?|guards?|spare\s+blades?)\b|\ba\s+spare\s+blade\b", "[配件数量，以用户资料为准]", masked, flags=re.I)
    masked = re.sub(r"\bChristmas\b", "[节日/送礼节点，以投放时间为准]", masked)
    return masked


def fmt_time(value: float) -> str:
    value = max(0.0, float(value))
    m, s = divmod(value, 60)
    return f"{int(m):02d}:{s:04.1f}"



PROFILE_LABELS = [
    ("style", "STYLE"),
    ("environment", "ENVIRONMENT"),
    ("tonePacing", "TONE & PACING"),
    ("camera", "CAMERA"),
    ("lighting", "LIGHTING"),
    ("character", "CHARACTER"),
    ("backgroundSound", "BACKGROUND SOUND"),
    ("transitionEditing", "TRANSITION / EDITING"),
]

_PROFILE_OVERRIDE_KEYS = {
    "style": ("style",),
    "environment": ("environment", "scene"),
    "tonePacing": ("tonePacing", "tone_pacing", "tone", "pacing"),
    "camera": ("cameraProfile", "camera_profile", "cameraStyle"),
    "lighting": ("lightingProfile", "lighting_profile"),
    "character": ("characterProfile", "character_profile"),
    "backgroundSound": ("backgroundSound", "background_sound", "sound", "music"),
    "transitionEditing": ("transitionEditing", "transition_editing", "editing"),
}

OUTDOOR_LABELS = {
    "sky", "tree", "trees", "plant", "plants", "mountain", "road", "outdoors",
    "grass", "vegetation", "landscape", "forest", "beach", "water", "field",
}


def count_spoken_words(text: str) -> int:
    if not text:
        return 0
    western = len(re.findall(r"[A-Za-z']+", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return western + cjk


def metric_stdev(metrics: list[dict], key: str) -> float | None:
    values = [float(x[key]) for x in metrics if isinstance(x.get(key), (int, float))]
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))


def profile_override(observations: dict, key: str) -> str:
    global_data = observations.get("global", {}) if isinstance(observations.get("global"), dict) else {}
    for candidate in _PROFILE_OVERRIDE_KEYS.get(key, (key,)):
        value = global_data.get(candidate)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def finalize_profile_entry(observations: dict, key: str, label: str, summary: str,
                           tags: list[str], confidence: str, evidence: list[str]) -> dict:
    override = profile_override(observations, key)
    if override:
        return {
            "label": label,
            "summary": override,
            "tags": tags,
            "confidence": "user",
            "evidence": evidence + ["用户已在 observations.global 中人工确认/覆盖"],
        }
    return {
        "label": label,
        "summary": summary,
        "tags": tags,
        "confidence": confidence,
        "evidence": evidence,
    }


def build_creative_profile(observations: dict, segments: list[dict], metrics: list[dict],
                           transcript: list[dict], full_text: str, cuts: list[float],
                           duration: float, ratio: str, kind: str, presence_mode: str,
                           person_segments: int, avg_cut: float,
                           brightness, saturation, contrast) -> dict:
    """Deterministically summarize the 8 whole-film creative dimensions.

    Local-only evidence: cut cadence, PIL brightness/saturation/contrast,
    Vision coarse labels, face/human counts, OCR density and transcript pace.
    Fine-grained facts (age, ethnicity, exact props, BGM genre, light source
    direction) stay conservative and are marked for user confirmation.
    """
    seg_count = len(segments)
    text_lower = full_text.lower()

    # ---- pace / speech ----
    speech_duration = sum(max(0.0, float(x["end"]) - float(x["start"])) for x in transcript)
    word_count = count_spoken_words(full_text)
    words_per_sec = (word_count / speech_duration) if speech_duration > 0.5 else 0.0
    if words_per_sec >= 2.7:
        pace_word = "语速偏快、信息密度高"
    elif words_per_sec >= 1.8:
        pace_word = "语速中等偏快、讲解连贯"
    elif word_count:
        pace_word = "语速舒缓、留白较多"
    else:
        pace_word = "无明显口播，靠画面和节奏推进"
    if avg_cut and avg_cut <= 3.0:
        cadence_word = "快速跳剪"
    elif avg_cut and avg_cut <= 6.0:
        pace_word = pace_word
        cadence_word = "节奏紧凑明快"
    else:
        cadence_word = "镜头停留较久、节奏平稳"
    first5_cuts = sum(1 for t in cuts if 0.5 < t < 5.0)
    hook_first = bool(segments and "开场钩子" in str(segments[0].get("purpose", "")))

    # ---- punctuation / delivery signals ----
    tone_tags = []
    if "?" in full_text or re.search(r"\b(why|what|how|stop|don'?t|never)\b", text_lower) or re.search(r"[？?]", full_text):
        tone_tags.append("设问/痛点直击")
    if "!" in full_text or "！" in full_text or re.search(r"\b(wow|oh my|amazing|guys|run|grab|love|obsessed)\b", text_lower):
        tone_tags.append("情绪外放/安利感")
    if re.search(r"\b(stop|wrong|don'?t|never|rash|hurt|problem|mistake)\b|别再|不要|踩坑|痛", text_lower):
        tone_tags.append("吐槽旧物/踩坑")
    if not tone_tags:
        tone_tags.append("直白讲解")

    # ---- environment coarse signal ----
    outdoor_segments = 0
    decor_segments = 0
    for seg in segments:
        keys = set(seg.get("label_counts", {}).keys())
        if keys & OUTDOOR_LABELS:
            outdoor_segments += 1
        if seg.get("decor_label_frames"):
            decor_segments += 1
    if outdoor_segments >= max(2, seg_count * 0.25):
        environment_summary = "户外/半户外生活化场景，背景、植被与空间关系以参考帧为准。"
        environment_conf = "medium"
        environment_tags = ["户外场景", "生活化"]
        env_evidence = [f"约 {outdoor_segments}/{seg_count} 个镜头出现户外类视觉粗标签"]
    else:
        decor_note = "，背景可见墙面、挂画或生活陈设" if decor_segments else "，背景为固定生活空间"
        environment_summary = (
            f"固定的生活化室内空间{decor_note}；房间类型、具体物件和布置以参考帧为准，不自行新增道具。"
        )
        environment_conf = "medium"
        environment_tags = ["室内居家", "固定场景"]
        env_evidence = [
            f"约 {decor_segments}/{seg_count} 个镜头检测到装饰/挂画类粗标签" if decor_segments else "未稳定检测到户外类粗标签，按室内固定空间处理",
            "具体房间与物件本地视觉模型不可靠，已标记以参考帧为准",
        ]

    # ---- camera / framing ----
    face_widths = [float(seg.get("max_face_width") or 0) for seg in segments]
    close_widths = [w for w in face_widths if w > 0]
    close_ratio = (sum(1 for w in close_widths if w >= 0.22) / len(close_widths)) if close_widths else 0.0
    if presence_mode == "none" or (presence_mode == "auto" and person_segments == 0):
        camera_summary = (
            f"{ratio} 竖屏商品展示视角，以商品为中心的近景/特写为主，机位和主体位置跟随参考帧，段落间硬切。"
        )
        camera_tags = ["商品中心构图", "近景特写", "硬切"]
    elif close_ratio >= 0.5:
        camera_summary = (
            f"全程近距离第一人称手机自拍视角，胸前到面部的中近景为主，穿插商品细节特写，"
            f"带自然的轻微手持晃动，段落间硬切，全 {seg_count} 镜。"
        )
        camera_tags = ["第一人称自拍", "中近景+商品特写", "自然手持感", "硬切"]
    else:
        camera_summary = (
            f"以中近景和商品特写为主的手机拍摄视角，机位、主体位置与手持/固定方式跟随参考帧，"
            f"段落间硬切，全 {seg_count} 镜。"
        )
        camera_tags = ["中近景", "商品特写", "硬切"]
    camera_evidence = [
        f"共 {seg_count} 个镜头，平均 {avg_cut:.1f}s/镜" if avg_cut else f"共 {seg_count} 个镜头",
        f"近距离人脸镜头占比约 {close_ratio:.0%}" if close_widths else "未稳定检测到正脸，机位以参考帧为准",
    ]

    # ---- lighting ----
    bright_std = metric_stdev(metrics, "brightness")
    even_word = "全片亮度较一致、受光均匀" if (bright_std is not None and bright_std <= 22) else "镜头间亮度有变化，切换处保持自然"
    lighting_summary = (
        f"{brightness_word(brightness)}、{saturation_word(saturation)}、{contrast_word(contrast)}，{even_word}；"
        "主光方向与软硬程度以参考帧为准，不额外加棚拍光效。"
    )
    lighting_tags = [brightness_word(brightness), saturation_word(saturation), contrast_word(contrast)]
    lighting_evidence = [
        f"平均亮度 {brightness:.1f}/255" if brightness is not None else "无亮度数据",
        f"平均饱和 {saturation:.1f}" if saturation is not None else "",
        f"平均对比 {contrast:.1f}" if contrast is not None else "",
        f"帧间亮度标准差 {bright_std:.1f}" if bright_std is not None else "",
    ]
    lighting_evidence = [x for x in lighting_evidence if x]

    # ---- character ----
    person_ratio = (person_segments / seg_count) if seg_count else 0.0
    # Local Vision frequently inflates face/human counts on OCR text and skin
    # texture (observed 4-10 false faces on a single-person selfie). Never claim
    # a multi-person cast from those counts; require explicit user confirmation.
    global_data = observations.get("global", {}) if isinstance(observations.get("global"), dict) else {}
    cast_hint = normalize_text(str(global_data.get("cast") or global_data.get("people_count") or ""))
    if presence_mode == "hands":
        character_summary = "只出现完成商品操作所需的一双手，不出现完整人物或正脸，手、指甲与袖口全片保持连续。"
        character_conf = "medium"
        character_tags = ["只露手", "无正脸"]
        character_evidence = ["出镜策略被设定为只露手"]
    elif presence_mode == "none" or (presence_mode == "auto" and person_segments == 0):
        character_summary = "全片不出现人物或正脸，镜头只围绕商品与使用环境。"
        character_conf = "medium"
        character_tags = ["无人出镜"]
        character_evidence = ["多数镜头未稳定检测到人物"]
    else:
        multi = bool(re.search(r"(多人|两人|三人|双人|\d\s*人|multi|two|three|2|3)", cast_hint)) and not re.search(r"(一人|单人|同一人|single|solo)", cast_hint)
        people_word = "多人同框" if multi else "同一人出镜"
        character_summary = (
            f"成年出镜者，{people_word}，约 {person_ratio:.0%} 镜头有人物，真实生活化、表情和语气有分享感；"
            "性别、年龄段、族裔、发型、妆容、纹身/首饰，以及是否多人同框，均以参考帧或用户补充为准，全片不换人、不换服装。"
        )
        character_conf = "medium"
        character_tags = ["真人出镜", people_word, "形象以参考帧为准"]
        character_evidence = [
            f"约 {person_segments}/{seg_count} 个镜头检测到人物",
            "本地 Vision 人脸计数在该样例明显偏高（单人自拍误报多张脸），不用于判定人数",
            "本地 Vision 无法可靠判断年龄/族裔/配饰，已标记待确认",
        ]

    # ---- style ----
    style_summary = (
        f"真实生活化的带货种草 UGC 风格（{kind}），手持手机拍摄的原生质感，真实肤色与材质，"
        "画面不过度磨皮美颜、不做成精致棚拍广告。"
    )
    style_tags = ["写实 UGC", "原生手机质感", kind]

    # ---- background sound ----
    if full_text:
        background_summary = (
            f"以博主清晰口播为主（{pace_word}，约 {words_per_sec:.1f} 词/秒）；"
            "背景乐与环境声以原片/用户后期素材为准，生成画面阶段只保留自然说话状态，口播后期配音对齐。"
        )
        background_tags = ["口播为主", "后期配音对齐"]
        bg_conf = "medium"
        bg_evidence = [
            f"检测到口播约 {word_count} 词/字，覆盖 {speech_duration:.1f}s，约 {words_per_sec:.1f} 词/秒",
            "当前未做音频能量分析，是否含 BGM 及其风格标记为以原片为准",
        ]
    else:
        background_summary = (
            "未稳定检测到口播，不新增旁白；BGM、商品音效或无声方案以原片和用户后期素材为准。"
        )
        background_tags = ["无口播", "BGM/音效后期决定"]
        bg_conf = "medium"
        bg_evidence = ["Whisper 未得到稳定口播文本", "是否含 BGM 标记为以原片为准"]

    # ---- transition / editing ----
    min_len = min((seg["end"] - seg["start"] for seg in segments), default=0.0)
    overlay_segments = sum(1 for seg in segments if any(not o.get("product_surface") for o in seg.get("ocr", [])))
    overlay_ratio = (overlay_segments / seg_count) if seg_count else 0.0
    caption_note = "，叠加文字/字幕随讲解同步出现" if overlay_ratio >= 0.3 else "，叠加文字较少"
    editing_summary = (
        f"短视频常见的{cadence_word}方式，{seg_count} 个硬切、平均 {avg_cut:.1f}s 一镜"
        f"（最短约 {min_len:.1f}s）{caption_note}，段落切换自然，不使用花哨转场特效。"
    )
    editing_tags = [cadence_word, f"{seg_count} 个硬切", "字幕随讲解" if overlay_ratio >= 0.3 else "少量字幕"]
    editing_evidence = [
        f"硬切镜头 {seg_count} 个，平均 {avg_cut:.1f}s，最短 {min_len:.1f}s",
        f"前 5 秒切点 {first5_cuts} 个（开场是否强切）",
        f"约 {overlay_ratio:.0%} 镜头检测到叠加文字",
    ]

    entries = {
        "style": ("STYLE", style_summary, style_tags, "medium",
                  [f"自动识别类型：{kind}", "UGC/棚拍判断基于人物口播+近景+真实质感的确定性特征"]),
        "environment": ("ENVIRONMENT", environment_summary, environment_tags, environment_conf, env_evidence),
        "tonePacing": ("TONE & PACING",
                       f"博主语气{('、'.join(dict.fromkeys(tone_tags)))}，{pace_word}，{cadence_word}；"
                       + ("前 3 秒用痛点/反差钩子抓人。" if hook_first else "开场进入相对直接。")
                       + "情绪强度的精确判断以参考帧为准。",
                       list(dict.fromkeys(tone_tags)) + [pace_word, cadence_word], "medium",
                       [f"约 {words_per_sec:.1f} 词/秒" if word_count else "无口播",
                        f"平均 {avg_cut:.1f}s/镜", f"前5秒切点 {first5_cuts} 个",
                        "情绪类型由标点/句式弱推断，精确情绪以参考帧为准"]),
        "camera": ("CAMERA", camera_summary, camera_tags, "medium", camera_evidence),
        "lighting": ("LIGHTING", lighting_summary, lighting_tags, "high", lighting_evidence),
        "character": ("CHARACTER", character_summary, character_tags, character_conf, character_evidence),
        "backgroundSound": ("BACKGROUND SOUND", background_summary, background_tags, bg_conf, bg_evidence),
        "transitionEditing": ("TRANSITION / EDITING", editing_summary, editing_tags, "high", editing_evidence),
    }

    profile = {}
    for key, label in PROFILE_LABELS:
        label2, summary, tags, conf, evidence = entries[key]
        profile[key] = finalize_profile_entry(observations, key, label, summary, tags, conf, evidence)
    return profile


def render(args) -> tuple[str, dict]:
    run = Path(args.run_dir)
    observations = load_json(Path(args.observations_file), {}) if args.observations_file else {}
    meta = load_json(run / "metadata.json", {})
    semantics = load_json(run / "local_semantics.json", {"frames": []})
    metrics = load_json(run / "visual_metrics.json", [])
    transcript = parse_srt(run / "audio.srt")
    ocr_items = parse_raw_ocr(run / "ocr_raw.txt")
    cuts = parse_cut_times(run / "cuts.log")
    frames = collect_frames(run / "frames")

    duration = float(meta.get("duration") or 0) or max((x["time"] for x in frames), default=0)
    width = int(meta.get("width") or 0)
    height = int(meta.get("height") or 0)
    ratio = aspect_label(width, height)
    aliases = brand_aliases(observations, args.brand_aliases or "", ocr_items)

    segments = build_segments(duration, cuts, frames, transcript)
    attach_visual_evidence(segments, semantics, ocr_items, aliases)
    apply_presence_override(segments, observations)
    global_presence = normalize_presence_mode(
        observations.get("global", {}).get("person_mode")
        or observations.get("global", {}).get("presence_mode")
        or observations.get("global", {}).get("people_mode")
        or observations.get("global", {}).get("subject_mode")
    )
    kind = infer_kind(segments, transcript, global_presence)
    intro_time = product_intro_time(transcript, observations)
    for seg in segments:
        seg["purpose"] = infer_purpose(seg, intro_time)

    product = observations.get("product", {}) if isinstance(observations.get("product"), dict) else {}
    product_name = args.product_name or product.get("name") or "[用户商品名]"
    product_image = args.product_image or product.get("image") or "[上传并绑定用户商品参考图]"
    product_notes = args.product_notes or product.get("notes") or "商品外观、颜色、形状、材质、按键、接口、数量、配件、包装和可见 logo 均以用户商品参考图为准。"
    selling_points = [str(x).strip() for x in product.get("selling_points", []) if str(x).strip()][:3] if isinstance(product.get("selling_points"), list) else []
    product_brief = {
        "use_scene": str(product.get("use_scene") or "").strip(),
        "target_audience": str(product.get("target_audience") or "").strip(),
        "price_offer": str(product.get("price_offer") or "").strip(),
        "cta": str(product.get("cta") or "").strip(),
        "banned_words": str(product.get("banned_words") or "").strip(),
    }
    pref_voice = str(observations.get("global", {}).get("voice_language") or "follow").strip().lower()
    pref_ratio = str(observations.get("global", {}).get("aspect_ratio") or "follow").strip()
    pref_strength = str(observations.get("global", {}).get("remix_strength") or "strong").strip().lower()
    if pref_ratio in {"9:16", "1:1", "16:9"}:
        ratio = pref_ratio
    source = meta.get("source_file") or meta.get("file_name") or run.name
    full_text = " ".join(x["text"] for x in transcript).strip()

    brightness = metric_avg(metrics, "brightness")
    saturation = metric_avg(metrics, "saturation")
    contrast = metric_avg(metrics, "contrast")
    avg_cut = duration / len(segments) if segments else 0
    person_segments = sum(1 for seg in segments if person_present(seg))
    has_cta = any("CTA" in seg["purpose"] for seg in segments)

    # Global facts. Local Vision is deliberately conservative here.
    decor_hits = sum(seg["decor_label_frames"] for seg in segments)
    adult_hits = sum(1 for seg in segments if seg["label_counts"].get("adult", 0) >= 0.5)
    default_scene = "固定使用参考帧中的同一空间和背景，墙面、家具、装饰物、桌面和生活道具的位置以参考帧为准；不要把场景改成影棚或另一个房间。"
    if decor_hits >= 3:
        default_scene = "固定室内空间，背景可见墙面挂画/装饰；具体墙面颜色、装饰物和家具只按参考帧复刻，不自行增加道具。"
    scene = obs_global(observations, "scene", default_scene)

    if global_presence == "person" or (global_presence == "auto" and person_segments):
        age_word = "成年" if adult_hits else "年龄感不确定的"
        character = obs_global(observations, "character", f"固定一名与参考帧一致的{age_word}出镜者；性别呈现、年龄、族裔、发型、妆容、纹身、首饰和面部配饰以参考帧或用户补充为准，全片不得换人。")
        wardrobe = obs_global(observations, "wardrobe", "服装严格沿用参考帧，颜色、版型、图案、发型和配饰在所有镜头保持连续。")
        fixed_subject = f"固定人物：{character} {wardrobe}"
    elif global_presence == "hands":
        character = obs_global(observations, "character", "只允许出现完成商品操作所需的一双手，不出现完整人物、正脸或身体；手、指甲、袖口和肤色保持连续。")
        wardrobe = obs_global(observations, "wardrobe", "如露出袖口，颜色和款式全片一致。")
        fixed_subject = f"出镜策略：{character} {wardrobe}"
    else:
        character = obs_global(observations, "character", "全片不新增人物、脸、身体或手持者；若参考帧本身无手，也不要生成手部。")
        wardrobe = obs_global(observations, "wardrobe", "无人出镜，不生成服装和人物配饰。")
        fixed_subject = f"出镜策略：{character} {wardrobe}"

    style = obs_global(observations, "style", f"写实商品视频，{kind}，保持原片 UGC/实拍质感、真实肤色和材质，不额外改成精致棚拍品牌广告。")
    camera = obs_global(observations, "camera", "机位、景别、主体位置、手持或固定方式严格跟随参考帧；段落间采用硬切，不新增花哨转场。")
    lighting = obs_global(observations, "lighting", f"{brightness_word(brightness)}、{saturation_word(saturation)}、{contrast_word(contrast)}，光线方向和阴影硬度参考原帧。")
    if full_text:
        audio = obs_global(observations, "audio", f"原片检测到口播；视频生成阶段只做自然说话状态，不要求单段精准对口型，后期用完整配音稿对齐。平均约 {avg_cut:.1f} 秒一个镜头。")
    else:
        audio = obs_global(observations, "audio", f"未稳定检测到口播；不要新增旁白，BGM/商品音效按用户后期素材或原片节奏处理。平均约 {avg_cut:.1f} 秒一个镜头。")

    story = observations.get("global", {}).get("story")
    if not story:
        ending = "，并以价格/优惠/CTA完成引导" if has_cta else "，最后强化商品外观或使用效果"
        if global_presence == "person" or (global_presence == "auto" and person_segments):
            story = f"出镜者在同一固定场景中，按参考视频的顺序完成需求铺垫、商品出场、使用证明和细节展示{ending}；商品替换为用户参考图中的「{product_name}」。"
        elif global_presence == "hands":
            story = f"一双手在固定场景中按参考视频节奏拿起、操作并展示「{product_name}」的外观、细节、使用方式和配套内容{ending}；不出现完整人物。"
        else:
            story = f"镜头按参考视频的节奏依次展示「{product_name}」的外观、细节、使用方式和场景效果{ending}，全程不额外添加人物。"

    creative_profile = build_creative_profile(
        observations, segments, metrics, transcript, full_text, cuts, duration, ratio, kind,
        global_presence, person_segments, avg_cut, brightness, saturation, contrast,
    )

    brief_parts = []
    if selling_points:
        brief_parts.append("已确认卖点（口播和画面优先表现）：" + "；".join(f"{i + 1}) {point}" for i, point in enumerate(selling_points)) + "。")
    if product_brief["use_scene"]:
        brief_parts.append(f"使用场景：{product_brief['use_scene']}。")
    if product_brief["target_audience"]:
        brief_parts.append(f"目标用户：{product_brief['target_audience']}。")
    if product_brief["price_offer"]:
        brief_parts.append(f"价格/优惠口径（用户已确认，可使用）：{product_brief['price_offer']}。")
    if product_brief["cta"]:
        brief_parts.append(f"行动号召（结尾 CTA 沿用）：{product_brief['cta']}。")
    brief_line = (" " + " ".join(brief_parts)) if brief_parts else ""

    global_prompt = "\n".join([
        f"生成一条 {ratio} 写实商品视频，总时长约 {duration:.1f} 秒，共 {len(segments)} 个硬切镜头，整体构图、景别、光线、色调、镜头运动、剪辑密度和商品展示顺序参考对标视频。{style}",
        fixed_subject,
        f"固定场景：{scene}",
        f"镜头与光线：{camera} {lighting}",
        f"主体商品为「{product_name}」，必须绑定用户商品参考图：{product_image}。{product_notes} 原片品牌、包装、logo、型号、参数、价格、折扣、配件和功效只作证据，不直接复制；没有用户确认就不生成。{brief_line}",
        "原片无人就不新增人物；只露手的视频不生成完整人物或正脸；真人视频则人物、服装、发型、配饰必须全片一致。商品不能融化、变形、结构错位、数量增减，也不能被手部遮挡关键外观。",
        f"声音：{audio}",
    ])

    constraints = [
        "不调用云端 LLM/VLM 补全未确认信息。",
        "不改变用户商品的形状、颜色、材质、比例、数量、按键、接口、包装和配件。",
        "原片品牌、包装和 logo 只作动作/构图参考；没有授权不复制到成片。",
        "不生成未提供的型号、参数、价格、折扣、销量、功效、库存、节日优惠或截止时间。",
        "不改变原片类型、人物数量、场景、画幅、字幕策略、声音类型和镜头节奏。",
        "原片无人就不新增人物；原片有人则保持同一人物、同一服装和同一配饰。",
        "每个镜头都必须有明确商品展示或销售任务，避免空泛氛围镜头。",
        "本地 Vision 的 people/adult/art 等粗标签不能当作精确年龄、族裔、品牌或场景结论。",
    ]
    if product_brief["banned_words"]:
        constraints.append(f"口播稿、屏幕文字和字幕不得出现以下禁用词：{product_brief['banned_words']}。")
    if pref_voice == "zh":
        constraints.append("配音语言：中文；口播稿与后期配音按中文处理，可意译原片口播，不逐字翻译。")
    elif pref_voice == "en":
        constraints.append("配音语言：英文；口播稿与后期配音按英文处理。")
    elif pref_voice == "other":
        constraints.append("配音语言：由用户后期指定；口播稿只保留结构和时长，不锁定语言。")
    if pref_ratio in {"9:16", "1:1", "16:9"} and pref_ratio != str(meta.get("ratio") or ""):
        constraints.append(f"用户指定输出画幅 {pref_ratio}；构图按 {pref_ratio} 重新取景，主体保持完整不裁切。")
    if pref_strength == "medium":
        constraints.append("复刻强度：中度；保留痛点→商品出场→证明→CTA 的销售结构和镜头时长节奏，允许替换场景、人物外观和具体道具。")
    elif pref_strength == "light":
        constraints.append("复刻强度：只参考打法；只沿用开钩方式、销售结构和时长节奏，镜头调度、场景和人物可以重新设计。")

    shot_prompts = []
    shot_records = []
    evidence_blocks = []
    for seg in segments:
        shot_obs = obs_shot(seg, observations)
        purpose = str(shot_obs.get("sales_task") or seg["purpose"])
        size = str(shot_obs.get("shot_size") or shot_obs.get("framing") or shot_size(seg, seg["purpose"]))
        action = str(shot_obs.get("visual_action") or shot_obs.get("action") or visual_action_fallback(seg, seg["purpose"], intro_time))
        phase = str(shot_obs.get("product_phase") or product_phase_note(seg, intro_time))
        texts, overlay_ocr, product_ocr = screen_texts_for_shot(seg, shot_obs)
        spoken_text = " ".join(x["text"] for x in seg["spoken"]).strip()
        if person_present(seg):
            human_note = "有人物出镜，保持同一人物、服装、发型和配饰"
        elif hands_only(seg):
            human_note = "只允许出现与参考帧一致的一双手和必要袖口，不生成正脸、完整身体或第二个人"
        else:
            human_note = "明确无人出镜，不添加人物、正脸、身体；若参考帧无手，也不要添加手"
        shot_camera = str(shot_obs.get("camera") or "")
        shot_lighting = str(shot_obs.get("lighting") or "")
        camera_full = " ".join(x for x in [camera, shot_camera] if x).strip()
        lighting_full = " ".join(x for x in [lighting, shot_lighting] if x).strip()

        frame_paths = [str(x["path"]) for x in seg["frames"]]
        subject_word = "人物位置" if person_present(seg) else ("手部位置" if hands_only(seg) else "主体位置")
        reference_line = "参考帧：按顺序绑定以下原帧，主体构图、" + subject_word + "、商品位置和背景关系以参考帧为准：" + "；".join(frame_paths)
        lines = [
            f"{ratio}，{size}，销售任务：{purpose}。",
            f"{scene} {human_note}。",
            f"画面动作：{action}",
            phase,
            f"镜头：{camera_full}",
            f"光线色彩：{lighting_full}",
            reference_line,
        ]
        if texts:
            lines.append(f"屏幕文字/UI：只保留以下内容并参考原帧位置与样式：{'；'.join(texts)}。")
        else:
            lines.append("屏幕文字/UI：本段不添加未经用户确认的新字幕、价格、标签或贴纸。")
        if spoken_text:
            lines.append(f"人物保持自然说话状态，声音留给后期；对应时间 {fmt_time(seg['start'])}–{fmt_time(seg['end'])}，配音见完整声音稿。")
        elif full_text:
            lines.append("本段没有独立转写句，但完整配音会沿时间轴连续覆盖；不要新增独立台词，保持人物自然说话或操作状态。")
        else:
            lines.append("原片未稳定检测到口播；本段不要新增旁白，只保留自然环境/商品动作声或交给后期处理。")
        lines.append("段尾硬切，保持快节奏，不使用花哨转场。")
        prompt = "\n".join(lines)
        shot_prompts.append((seg, purpose, prompt))

        shot_records.append({
            "index": seg["index"],
            "start": round(float(seg["start"]), 3),
            "end": round(float(seg["end"]), 3),
            "duration": round(max(0.0, float(seg["end"]) - float(seg["start"])), 3),
            "purpose": purpose,
            "framing": size,
            "visualAction": action,
            "productPhase": phase,
            "presence": "person" if person_present(seg) else ("hands" if hands_only(seg) else "none"),
            "scene": scene,
            "humanNote": human_note,
            "camera": camera_full,
            "lighting": lighting_full,
            "screenTexts": texts,
            "voiceover": spoken_text,
            "voiceoverSegments": [
                {
                    "start": round(float(x["start"]), 3),
                    "end": round(float(x["end"]), 3),
                    "text": x["text"],
                }
                for x in seg["spoken"]
            ],
            "referenceFrames": [
                {"time": int(x["time"]), "path": str(x["path"])}
                for x in seg["frames"]
            ],
            "ocrEvidence": {
                "overlay": [
                    {
                        "text": x["text"],
                        "confidence": round(float(x["confidence"]), 4),
                        "x": round(float(x["x"]), 4),
                        "y": round(float(x["y"]), 4),
                        "count": int(x.get("count", 1)),
                    }
                    for x in overlay_ocr
                ],
                "productSurface": [
                    {
                        "text": x["text"],
                        "confidence": round(float(x["confidence"]), 4),
                        "x": round(float(x["x"]), 4),
                        "y": round(float(x["y"]), 4),
                        "count": int(x.get("count", 1)),
                    }
                    for x in product_ocr
                ],
            },
            "visionLabels": seg["labels"],
            "visionLabelConfidence": {
                name: round(float(value), 4)
                for name, value in sorted(seg["label_counts"].items(), key=lambda kv: kv[1], reverse=True)
            },
            "faceCount": int(seg["face_count"]),
            "humanCount": int(seg["human_count"]),
            "prompt": prompt,
        })

        ev = [f"#### Evidence Shot {seg['index']}｜{fmt_time(seg['start'])}–{fmt_time(seg['end'])}｜{purpose}"]
        ev.append("- **参考原帧**：" + "；".join(f"`{p}`" for p in frame_paths))
        if seg["labels"]:
            ev.append(f"- **本地 Vision 粗标签（不是精确身份判断）**：{', '.join(seg['labels'])}")
        if overlay_ocr:
            ev.append("- **原片叠加文字 OCR**：" + "；".join(f"{x['text']}（conf={x['confidence']:.2f}, y={x['y']:.2f}, n={x['count']}）" for x in overlay_ocr))
        else:
            ev.append("- **原片叠加文字 OCR**：无高置信字幕")
        if product_ocr:
            ev.append("- **原片商品/包装表面字样（不直接复制）**：" + "；".join(f"{x['text']}（conf={x['confidence']:.2f}, y={x['y']:.2f}, n={x['count']}）" for x in product_ocr))
        if spoken_text:
            ev.append(f"- **原片声音证据**：{spoken_text}")
        else:
            ev.append("- **原片声音证据**：该段无独立转写句，按完整声音稿/BGM/音效对齐")
        if shot_obs:
            ev.append("- **已采用用户 observations**：人物/动作/场景/字幕等字段已并入本镜提示词")
        evidence_blocks.append("\n".join(ev))

    master_parts = [f"【全局】\n{global_prompt}"]
    for seg, purpose, prompt in shot_prompts:
        master_parts.append(f"【镜头 {seg['index']}｜{fmt_time(seg['start'])}–{fmt_time(seg['end'])}｜{purpose}】\n{prompt}")
    profile_lines = ["【风格设定（复刻全片基调，需与商品参考图一起绑定）】"]
    for _profile_key, _profile_label in PROFILE_LABELS:
        _entry = creative_profile[_profile_key]
        profile_lines.append(f"{_profile_label}: {_entry['summary']}")
    master_parts.append("\n".join(profile_lines))

    master_parts.append("【硬约束】\n" + "\n".join(f"- {x}" for x in constraints))
    if full_text:
        master_parts.append("【声音后期】\n视频生成阶段不依赖模型现场说清完整口播；先用自然说话状态生成画面，再用用户确认后的配音稿、BGM 和字幕在后期对齐。")
    else:
        master_parts.append("【声音后期】\n未稳定检测到口播；不要新增旁白，按用户提供的 BGM、商品音效或无声方案后期处理。")
    master_prompt = "\n\n".join(master_parts)

    result_payload = {
        "schemaVersion": "video-analysis.result/v1",
        "meta": {
            "source": str(source),
            "evidenceDir": str(run.resolve()),
            "duration": round(float(duration), 3),
            "width": width,
            "height": height,
            "ratio": ratio,
            "kind": kind,
            "personMode": global_presence,
            "shotCount": len(segments),
            "averageShotDuration": round(float(avg_cut), 3),
            "cloudLlmUsed": False,
            "localPipeline": ["ffmpeg", "ffprobe", "local-whisper", "macos-vision", "pil", "deterministic-python"],
        },
        "product": {
            "name": product_name,
            "referenceImage": product_image,
            "notes": product_notes,
        },
        "story": story,
        "production": {
            "style": style,
            "scene": scene,
            "character": character,
            "wardrobe": wardrobe,
            "camera": camera,
            "lighting": lighting,
            "audio": audio,
        },
        "creativeProfile": creative_profile,
        "globalPrompt": global_prompt,
        "simplePrompt": master_prompt,
        "shots": shot_records,
        "voiceover": {
            "source": full_text,
            "masked": mask_voiceover(full_text, aliases) if full_text else "",
            "segments": [
                {
                    "start": round(float(x["start"]), 3),
                    "end": round(float(x["end"]), 3),
                    "text": x["text"],
                }
                for x in transcript
            ],
        },
        "constraints": constraints,
    }

    out = []
    out.append("# Seedance 商品视频生成稿（简版投喂 + 本地证据详情）")
    out.append("")
    out.append("## 成片信息")
    out.append("")
    out.append(f"- 对标视频：`{source}`")
    out.append(f"- 自动识别类型：{kind}")
    out.append(f"- 用户商品：{product_name}")
    out.append(f"- 商品参考图：`{product_image}`")
    out.append(f"- 输出比例：{ratio}（沿用原片）")
    out.append(f"- 总时长：约 {duration:.1f}s；{len(segments)} 个生成镜头，平均 {avg_cut:.1f}s/段")
    out.append("- 生成方式：本地 Whisper + macOS Vision + ffmpeg/ffprobe + PIL + 确定性组稿；未调用云端 LLM/VLM。")
    out.append("")
    out.append("## Story 一句话")
    out.append("")
    out.append(f"> {story}")
    out.append("")
    out.append("## 简版总提示词（一次性复制给 Seedance；需同时绑定商品图和各镜头参考原帧）")
    out.append("")
    out.append("```text")
    out.append(master_prompt)
    out.append("```")
    out.append("")
    out.append("## 分段提示词（逐段生成时复制）")
    out.append("")
    out.append("### 全局提示词")
    out.append("")
    out.append("```text")
    out.append(global_prompt)
    out.append("```")
    out.append("")
    for seg, purpose, prompt in shot_prompts:
        out.append(f"### Shot {seg['index']}｜{fmt_time(seg['start'])}–{fmt_time(seg['end'])}｜{purpose}")
        out.append("")
        out.append("```text")
        out.append(prompt)
        out.append("```")
        out.append("")

    out.append("## 复刻风格卡（8 块｜用户可读，置信度供人工复核）")
    out.append("")
    for _profile_key, _profile_label in PROFILE_LABELS:
        _entry = creative_profile[_profile_key]
        _conf_label = {"high": "高", "medium": "中", "low": "低", "user": "用户确认"}.get(_entry["confidence"], _entry["confidence"])
        out.append(f"**{_profile_label}**（置信度：{_conf_label}）")
        out.append("")
        out.append(f"{_entry['summary']}")
        if _entry.get("tags"):
            out.append("")
            out.append("- 标签：" + "、".join(_entry["tags"]))
        out.append("")
    out.append("---")
    out.append("")
    out.append("# 以下为详情/证据区，不建议直接投喂生成模型")
    out.append("")
    out.append("## 参考帧绑定清单")
    out.append("")
    for seg, _, _ in shot_prompts:
        paths = [str(x["path"]) for x in seg["frames"]]
        out.append(f"- Shot {seg['index']}：" + "；".join(f"`{p}`" for p in paths))
    out.append("")
    out.append("## 分镜证据详情")
    out.append("")
    out.extend(evidence_blocks)
    out.append("")

    out.append("## 声音稿")
    out.append("")
    if full_text:
        out.append("### 原片声音证据（只作语速/节奏参考）")
        out.append("")
        out.append(f"> {full_text}")
        out.append("")
        out.append("### 成片配音占位模板（品牌、价格、规格和功效必须用用户资料替换）")
        out.append("")
        out.append(f"> {mask_voiceover(full_text, aliases)}")
    else:
        out.append("> 未获得口播文本。按原片 BGM、商品音效或无声处理；不要自行新增旁白。")
    out.append("")

    out.append("## 反向约束")
    out.append("")
    out.extend([f"- {x}" for x in constraints])
    out.append("")

    out.append("## 建议补充给本地生成器的素材")
    out.append("")
    for item in [
        "商品正面、侧面、背面、细节、配件和包装图",
        "固定人物参考图（如需要同一 AI 人物）",
        "结构化 observations.json（人物、服装、场景、逐镜动作、成片字幕等）",
        "最终商品名、1–3 个已确认卖点、价格、优惠和 CTA 文案",
        "目标语言、口音、地区和投放平台",
    ]:
        out.append(f"- {item}")
    out.append("")
    return "\n".join(out), result_payload


def write_simple_prompt(markdown: str, path: str):
    if not path:
        return
    marker = "## 简版总提示词"
    if marker not in markdown:
        return
    tail = markdown.split(marker, 1)[1]
    m = re.search(r"```text\n(.*?)\n```", tail, re.S)
    if m:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(m.group(1).strip() + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir")
    parser.add_argument("--product-name", default="")
    parser.add_argument("--product-image", default="")
    parser.add_argument("--product-notes", default="")
    parser.add_argument("--observations-file", default="")
    parser.add_argument("--brand-aliases", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--simple-output", default="")
    parser.add_argument("--json-output", default="")
    args = parser.parse_args()
    text, payload = render(args)
    if args.json_output:
        json_path = Path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        write_simple_prompt(text, args.simple_output)
    else:
        print(text)


if __name__ == "__main__":
    main()
