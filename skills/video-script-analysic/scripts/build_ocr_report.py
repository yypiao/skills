#!/usr/bin/env python3
"""Convert raw Vision OCR output into a timestamped Markdown evidence file."""
import re
import sys
from pathlib import Path


def timestamp_for(filename: str) -> str:
    match = re.match(r'(hook|s|sec|t|cut)_(\d+)\.(?:jpe?g|png)$', filename, re.I)
    if not match:
        return ''
    kind, index = match.groups()
    index = int(index) - 1
    seconds = {
        'hook': index,
        's': index,
        'sec': index,
        't': index * 3,
        'cut': index,  # cut files are replaced by cuts.log downstream when available
    }.get(kind, 0)
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


def main(raw_path: str, out_path: str, cuts_log: str = '') -> None:
    cut_times = []
    cuts = Path(cuts_log) if cuts_log else None
    if cuts is not None and cuts.is_file():
        cut_times = [float(x) for x in re.findall(r'pts_time:([0-9.]+)', cuts.read_text(errors='ignore'))]

    current = None
    groups = []
    for line in Path(raw_path).read_text(encoding='utf-8', errors='ignore').splitlines():
        if line.startswith('### '):
            if current:
                groups.append(current)
            current = {'file': line[4:].strip(), 'lines': []}
        elif current and line.strip():
            current['lines'].append(line.strip())
    if current:
        groups.append(current)

    cut_index = 0
    rendered = []
    for group in groups:
        filename = group['file']
        ts = timestamp_for(filename)
        if filename.startswith('cut_') and cut_index < len(cut_times):
            seconds = int(cut_times[cut_index])
            ts = f'{seconds // 60:02d}:{seconds % 60:02d}'
            cut_index += 1
        text_lines = []
        for item in group['lines']:
            item = re.sub(r'^\[confidence=[^\]]+\]\s*', '', item)
            text_lines.append(item)
        if text_lines:
            heading = f'## {ts} `{filename}`' if ts else f'## `{filename}`'
            rendered.append(heading)
            rendered.extend(f'- {x}' for x in text_lines)
            rendered.append('')

    Path(out_path).write_text('\n'.join(rendered).strip() + ('\n' if rendered else ''), encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4):
        raise SystemExit('usage: build_ocr_report.py raw_ocr.txt ocr.md [cuts.log]')
    cuts_log = sys.argv[3] if len(sys.argv) == 4 and Path(sys.argv[3]).is_file() else ''
    main(sys.argv[1], sys.argv[2], cuts_log)
