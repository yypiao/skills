#!/usr/bin/env python3
"""Quantify sampled video frames when direct image input is unavailable."""
import colorsys
import json
import re
import sys
from pathlib import Path
from PIL import Image, ImageStat


def frame_time(path: Path):
    m = re.match(r'(hook|s|sec|t)_(\d+)\.(?:jpe?g|png)$', path.name, re.I)
    if not m:
        return None
    kind, i = m.groups()
    i = int(i) - 1
    if kind in ('hook', 's', 'sec'):
        return i
    return i * 3


def analyze(path: Path):
    with Image.open(path).convert('RGB') as im:
        small = im.resize((96, 160)) if im.height >= im.width else im.resize((160, 96))
        pixels = list(small.getdata())
        stat = ImageStat.Stat(im)
        mean_rgb = stat.mean
        brightness = sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in [mean_rgb])
        hsv = [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255) for r, g, b in pixels]
        saturation = sum(s for _, s, _ in hsv) / len(hsv) * 100
        dominant = im.resize((1, 1)).getpixel((0, 0))
        grayscale = im.convert('L')
        gstat = ImageStat.Stat(grayscale)
        contrast = gstat.stddev[0]
        return {
            'brightness': round(brightness, 2),
            'saturation': round(saturation, 2),
            'contrast': round(contrast, 2),
            'dominant_rgb': dominant,
        }


def main(frames_dir: str, out_json: str, out_md: str):
    records = []
    previous = None
    for path in sorted(Path(frames_dir).glob('s_*.jpg')):
        t = frame_time(path)
        if t is None:
            continue
        item = {'time': t, 'file': path.name, **analyze(path)}
        if previous is not None:
            item['change_from_previous'] = round(abs(item['brightness'] - previous), 2)
        previous = item['brightness']
        records.append(item)

    Path(out_json).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    if not records:
        Path(out_md).write_text('无可用采样帧。\n', encoding='utf-8')
        return

    avg_brightness = sum(x['brightness'] for x in records) / len(records)
    avg_saturation = sum(x['saturation'] for x in records) / len(records)
    high_change = [x for x in records if x.get('change_from_previous', 0) >= 18]
    lines = [
        '# 画面量化摘要',
        '',
        f'- 采样帧数:{len(records)}',
        f'- 平均亮度:{avg_brightness:.1f}/255',
        f'- 平均饱和度:{avg_saturation:.1f}/100',
        f'- 明显亮度跳变:{len(high_change)} 次',
        '',
        '| 时间 | 文件 | 亮度 | 饱和度 | 对比度 | 主色RGB | 与上帧亮度差 |',
        '| --- | --- | ---: | ---: | ---: | --- | ---: |',
    ]
    for x in records:
        mm, ss = divmod(x['time'], 60)
        lines.append(
            f"| {mm:02d}:{ss:02d} | `{x['file']}` | {x['brightness']:.1f} | "
            f"{x['saturation']:.1f} | {x['contrast']:.1f} | {x['dominant_rgb']} | "
            f"{x.get('change_from_previous', '-')} |"
        )
    Path(out_md).write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('usage: analyze_frames.py frames_dir visual_metrics.json visual_metrics.md')
    main(*sys.argv[1:])
