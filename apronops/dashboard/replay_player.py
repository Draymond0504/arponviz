"""Portable, network-free canvas animation for validated experiment replays."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from apronops.experiments.replay import load_replay


def build_replay_html(data):
    # Raw JSON is placed in a script text node. Escape HTML metacharacters so
    # uploaded labels cannot terminate it; JS renders user strings as text only.
    payload = {key: data[key] for key in ('manifest', 'layout', 'frames', 'summary')}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    for old, new in (('&', r'\u0026'), ('<', r'\u003c'), ('>', r'\u003e')):
        encoded = encoded.replace(old, new)
    template = Path(__file__).with_name('replay_player.html').read_text(encoding='utf-8')
    return template.replace('__REPLAY_DATA__', encoded)


def main():
    parser = argparse.ArgumentParser(description='将 replay.json 导出为可离线播放的 HTML 动画')
    parser.add_argument('replay', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.replay.stat().st_size > 100 * 1024 * 1024:
        parser.error('回放文件超过 100 MB')
    html = build_replay_html(load_replay(args.replay.read_bytes()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Do not silently overwrite another exported animation.
    with args.output.open('x', encoding='utf-8') as output:
        output.write(html)
    print(args.output.resolve())


if __name__ == '__main__':
    main()
