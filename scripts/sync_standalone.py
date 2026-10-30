#!/usr/bin/env python3
"""Refresh/check copyable scripts and skill assets from canonical package files."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
SKILL = Path('skills/creative/comfyui-matrix-videos')
PAIRS = [
    ('src/comfyui_matrix_videos/cli.py', 'scripts/comfy_matrix_video.py'),
    ('src/comfyui_matrix_videos/manual.py', 'scripts/manual_matrix_video.py'),
    ('src/comfyui_matrix_videos/cli.py', SKILL / 'scripts/comfy_matrix_video.py'),
    ('src/comfyui_matrix_videos/manual.py', SKILL / 'scripts/manual_matrix_video.py'),
    ('docs/manual-mode.md', SKILL / 'references/manual-mode.md'),
    *[(f'templates/{name}', SKILL / 'templates' / name) for name in
      ('matrix_style.example.json', 'manual_sources.example.json', 'manual_design.example.json')],
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    mismatches = []
    for source, destination in PAIRS:
        src, dst = ROOT / source, ROOT / destination
        if args.check:
            if not dst.is_file() or src.read_bytes() != dst.read_bytes():
                mismatches.append(str(destination))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    if mismatches:
        parser.exit(1, 'Out-of-date bundle files: ' + ', '.join(mismatches) + '\n')
    print(f'{len(PAIRS)} bundled files ' + ('match canonical sources' if args.check else 'refreshed'))


if __name__ == '__main__':
    main()
