#!/usr/bin/env python3
"""Create four tiny synthetic clips and portable manual-mode configs."""
import argparse
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path, help='New sample directory (must not exist)')
    args = parser.parse_args()
    root = args.directory.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / 'media').mkdir()
    templates = Path(__file__).resolve().parents[1] / 'templates'
    for name in ('sources', 'design'):
        shutil.copyfile(templates / f'manual_{name}.example.json', root / f'{name}.json')
    for color in ('red', 'blue', 'green', 'yellow'):
        command = ['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-threads', '2',
                   '-filter_threads', '1', '-filter_complex_threads', '1', '-f', 'lavfi',
                   '-i', f'color={color}:s=128x96:r=12:d=1']
        if color == 'red':
            command += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1']
        command += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-threads:v', '2',
                    '-c:a', 'aac', '-threads:a', '2', str(root / 'media' / f'{color}.mp4')]
        if shutil.which('nice'):
            command = ['nice', '-n', '10', *command]
        subprocess.run(command, check=True)
    print(f'Created synthetic clips and sources.json/design.json in {root}')
    print('Silent by default; set audio_source_index to 1 to use the reference tone.')


if __name__ == '__main__':
    main()
