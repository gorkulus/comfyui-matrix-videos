#!/usr/bin/env python3
"""Ordered native-video grids, independent of parameter-sweep manifests."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

PREPARATION_VERSION = 1
COMPOSITION_VERSION = 1
DEFAULTS = {
    'gutter': 8, 'margin': 0, 'caption_height': 32, 'square': False,
    'label_position': 'caption', 'label_align': 'center', 'font_size': 18,
    'font': '', 'background': '#141416', 'text_color': '#eeeeee',
    'audio_source_index': None, 'audio_sample_rate': 48000, 'audio_channels': 2,
    'loops': [1], 'preset': 'medium', 'crf': 18,
}
REQUIRED = {'rows', 'columns', 'cell_width', 'cell_height', 'fps', 'frames'}


class ManualError(ValueError):
    """Invalid configuration, source media or cached artifact."""


def require(ok, message):
    if not ok:
        raise ManualError(message)


def integer(value, name, minimum=0, even=False):
    require(type(value) is int and value >= minimum, f'{name} must be an integer >= {minimum}')
    require(not even or value % 2 == 0, f'{name} must be even for yuv420p')
    return value


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def resolve_file(value, parent):
    p = Path(value).expanduser()
    p = (parent / p).resolve() if not p.is_absolute() else p.resolve()
    require(p.is_file(), f'File not found: {p}')
    return p


@dataclass
class Plan:
    sources: list
    design: dict
    canvas: tuple
    rects: list
    duration: Fraction
    inputs: list


def load_plan(sources_path, design_path):
    sources_path, design_path = Path(sources_path).resolve(), Path(design_path).resolve()
    manifest = json.loads(sources_path.read_text(encoding='utf-8'))
    raw = json.loads(design_path.read_text(encoding='utf-8'))
    require(isinstance(raw, dict), 'Design must be a JSON object')
    require(REQUIRED <= raw.keys(), f'Design requires: {", ".join(sorted(REQUIRED))}')
    require(not raw.keys() - REQUIRED - DEFAULTS.keys() - {'margins'}, 'Unknown design field')
    d = {**DEFAULTS, **raw}
    for field in ('rows', 'columns', 'frames', 'font_size', 'audio_sample_rate', 'audio_channels'):
        integer(d[field], field, 1)
    require(d['audio_channels'] in (1, 2), 'audio_channels must be 1 or 2')
    require(d['audio_sample_rate'] in (32000, 44100, 48000), 'audio_sample_rate must be 32000, 44100 or 48000')
    for field in ('cell_width', 'cell_height'):
        integer(d[field], field, 2, even=True)
    for field in ('gutter', 'margin', 'caption_height'):
        integer(d[field], field, even=True)
    try:
        fps = Fraction(str(d['fps']))
    except (ValueError, ZeroDivisionError) as exc:
        raise ManualError('fps must be a positive rational, e.g. "30000/1001"') from exc
    require(0 < fps <= 240, 'fps must be positive and <= 240')
    d['fps'] = str(fps)
    duration = Fraction(d['frames'], 1) / fps
    require((duration * d['audio_sample_rate']).denominator == 1 or d['audio_source_index'] is None,
            'Duration must be a whole number of audio samples when audio is selected')
    require(isinstance(d['loops'], list) and bool(d['loops']), 'loops must be a nonempty list')
    for loops in d['loops']:
        integer(loops, 'loops entry', 1)
    require(len(set(d['loops'])) == len(d['loops']), 'loops entries must be unique')
    require(type(d['square']) is bool, 'square must be boolean')
    require(not (d['square'] and 'margins' in raw), 'square cannot be combined with explicit margins')
    require(d['label_position'] in ('caption', 'top_header', 'none'), 'Invalid label_position')
    require(d['label_align'] in ('left', 'center', 'right'), 'Invalid label_align')
    integer(d['crf'], 'crf')
    require(d['crf'] <= 51, 'crf must be <= 51')
    require(d['preset'] in ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'), 'Invalid preset')
    for color in ('background', 'text_color'):
        require(isinstance(d[color], str) and re.fullmatch(r'#[0-9a-fA-F]{6}', d[color]), f'{color} must be #RRGGBB')
    require(isinstance(manifest, dict) and isinstance(manifest.get('sources'), list), 'Expected an object with an ordered sources array')
    sources = manifest['sources']
    require(len(sources) == d['rows'] * d['columns'], 'Source count must equal rows * columns (no implicit blank cells)')
    if 'expected_count' in manifest:
        integer(manifest['expected_count'], 'expected_count', 1)
        require(manifest['expected_count'] == len(sources), 'expected_count does not match sources')
    if d['audio_source_index'] is not None:
        integer(d['audio_source_index'], 'audio_source_index', 1)
        require(d['audio_source_index'] <= len(sources), 'audio_source_index exceeds source count')
    records = []
    for s in sources:
        require(isinstance(s, dict) and isinstance(s.get('path'), str) and bool(s['path']), 'Every source needs a path string')
        require(not s.keys() - {'path', 'label', 'preparation'}, 'Unknown source field')
        label = s.get('label', '')
        require(isinstance(label, str) and '\x00' not in label, 'label must be a string without NUL')
        prep = s.get('preparation') or {}
        require(isinstance(prep, dict) and not prep.keys() - {'expected_source_frames', 'end_fade_ms', 'align_audio_to_video_start'}, 'Unknown preparation field')
        if 'expected_source_frames' in prep:
            integer(prep['expected_source_frames'], 'expected_source_frames', d['frames'])
        try:
            fade = Fraction(str(prep.get('end_fade_ms', 0)))
        except (ValueError, ZeroDivisionError) as exc:
            raise ManualError('end_fade_ms must be a nonnegative number') from exc
        require(0 <= fade <= duration * 1000, 'end_fade_ms exceeds duration or is negative')
        require(type(prep.get('align_audio_to_video_start', False)) is bool, 'align_audio_to_video_start must be boolean')
        records.append({'path': resolve_file(s['path'], sources_path.parent), 'label': label, 'preparation': prep})
    margins = d.get('margins', {})
    require(isinstance(margins, dict) and not margins.keys() - {'left', 'right', 'top', 'bottom'}, 'Invalid margins')
    margins = {side: integer(margins.get(side, d['margin']), f'margins.{side}', even=True)
               for side in ('left', 'right', 'top', 'bottom')}
    w, h, gap, cap = (d[k] for k in ('cell_width', 'cell_height', 'gutter', 'caption_height'))
    gw = d['columns'] * w + (d['columns'] - 1) * gap
    gh = d['rows'] * (h + cap) + (d['rows'] - 1) * gap
    width, height = gw + margins['left'] + margins['right'], gh + margins['top'] + margins['bottom']
    x, y = margins['left'], margins['top']
    if d['square']:
        width = height = max(width, height)
        # Chroma-aligned centering; any two-pixel remainder goes right/bottom.
        x, y = ((width - gw) // 4) * 2, ((height - gh) // 4) * 2
    if d['label_position'] == 'top_header':
        require(cap == 0, 'top_header requires caption_height=0')
        require(all(not s['label'] for s in records[d['columns']:]), 'Header labels must belong to first row sources only')
        require(y >= d['font_size'] + 8, 'Top margin must fit font_size plus 8 pixels')
    if d['label_position'] == 'caption' and any(s['label'] for s in records):
        require(cap >= d['font_size'] + 4, 'caption_height must fit font_size plus 4 pixels')
    if d['font']:
        require(isinstance(d['font'], str), 'font must be a local font path')
        d['font'] = str(resolve_file(d['font'], design_path.parent))
    rects = [(x + i % d['columns'] * (w + gap), y + i // d['columns'] * (h + cap + gap), w, h)
             for i in range(len(records))]
    return Plan(records, d, (width, height), rects, duration,
                [sources_path, design_path] + ([Path(d['font'])] if d['font'] else []))


def run(args):
    command = list(map(str, args))
    if os.name == 'posix' and shutil.which('nice'):
        command = ['nice', '-n', '10', *command]
    p = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise ManualError(f'{args[0]} failed: {p.stderr.decode(errors="replace").strip()}')
    return p.stdout


def ff(args):
    return run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-y',
                '-threads', '2', '-filter_threads', '1', '-filter_complex_threads', '1', *args])


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path]))


def frame_hashes(path):
    text = ff(['-i', path, '-map', '0:v:0', '-an', '-threads', '2', '-f', 'framemd5', '-']).decode()
    return [line.split(',')[-1].strip() for line in text.splitlines() if line and not line.startswith('#')]


def check_video(path, d, count, dimensions):
    data = probe(path)
    v = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    if v is None:
        raise ManualError(f'No video stream: {path}')
    require(v.get('sample_aspect_ratio', '1:1') in ('1:1', '0:1', 'N/A'),
            f'Only square-pixel sources are supported: {path}')
    require(not any(s.get('rotation', 0) for s in v.get('side_data_list', [])),
            f'Rotated display metadata is not supported; normalize a copy first: {path}')
    require((v['width'], v['height']) == dimensions, f'Native dimensions mismatch: {path}; expected {dimensions}')
    require(Fraction(v['avg_frame_rate']) == Fraction(d['fps']), f'FPS mismatch: {path}; expected {d["fps"]}')
    frames = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
                             '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', path]))['frames']
    require(len(frames) == count, f'Frame count mismatch: {path}; expected {count}, got {len(frames)}')
    times = [Fraction(f['best_effort_timestamp_time']) for f in frames]
    # FFprobe text rounds to microseconds; Matroska stores milliseconds.
    tolerance = max(Fraction(v['time_base']) / 2, Fraction(1, 1000000))
    require(all(abs(t - times[0] - Fraction(i) / Fraction(d['fps'])) <= tolerance for i, t in enumerate(times)),
            f'Variable frame timing is not supported: {path}')
    ff(['-xerror', '-i', path, '-map', '0:v:0', '-an', '-threads', '2', '-f', 'null', '-'])
    return data


def preparation_key(source_sha, source, d, ffmpeg_version):
    return key({'version': PREPARATION_VERSION, 'source': source_sha, 'preparation': source['preparation'],
                'cell_width': d['cell_width'], 'cell_height': d['cell_height'],
                'frames': d['frames'], 'fps': d['fps'], 'sample_rate': d['audio_sample_rate'],
                'channels': d['audio_channels'], 'ffmpeg': ffmpeg_version})


def prepare(plan, cache, stage):
    d = plan.design
    version = run(['ffmpeg', '-version']).decode().splitlines()[0]
    records = []
    for index, source in enumerate(plan.sources, 1):
        sha = digest(source['path'])
        cache_key = preparation_key(sha, source, d, version)
        folder = cache / 'prepared' / cache_key
        meta = folder / 'record.json'
        if meta.exists():
            record = json.loads(meta.read_text())
            for field, filename in (('video_sha256', 'video.nut'), ('audio_sha256', 'audio.f32le')):
                if record.get(field):
                    require((folder / filename).is_file() and digest(folder / filename) == record[field],
                            f'Corrupt preparation cache: {folder}; remove this entry and prepare again')
        else:
            require(stage != 'compose', f'Preparation cache missing for source {index}; run --stage prepare or all')
            folder.mkdir(parents=True, exist_ok=True)
            prep = source['preparation']
            expected = prep.get('expected_source_frames', d['frames'])
            data = check_video(source['path'], d, expected, (d['cell_width'], d['cell_height']))
            v = next(s for s in data['streams'] if s['codec_type'] == 'video')
            video = folder / 'video.nut'
            ff(['-i', source['path'], '-map', '0:v:0', '-an', '-vf',
                f'trim=end_frame={d["frames"]},setpts=N/({d["fps"]}*TB)',
                '-fps_mode', 'passthrough', '-c:v', 'ffv1', '-level', '3', '-pix_fmt', v['pix_fmt'], '-threads', '2', video])
            source_hashes = frame_hashes(source['path'])[:d['frames']]
            require(frame_hashes(video) == source_hashes, f'Lossless preparation changed retained decoded pixels: {source["path"]}')
            audio = next((s for s in data['streams'] if s['codec_type'] == 'audio'), None)
            pcm = folder / 'audio.f32le'
            audio_sha = None
            if audio:
                samples = int(plan.duration * d['audio_sample_rate'])
                offset = Fraction(0)
                if prep.get('align_audio_to_video_start', False):
                    offset = Fraction(v.get('start_time', '0')) - Fraction(audio.get('start_time', '0'))
                    require(offset >= 0, 'Audio starts after video; explicit delayed-audio alignment is not supported')
                af = [f'atrim=start={float(offset):.12f}', 'asetpts=PTS-STARTPTS',
                      f'aresample={d["audio_sample_rate"]}', f'apad=whole_len={samples}', f'atrim=end_sample={samples}']
                fade = round(Fraction(str(prep.get('end_fade_ms', 0))) * d['audio_sample_rate'] / 1000)
                if fade:
                    af.append(f'afade=t=out:ss={samples-fade}:ns={fade}:curve=tri')
                ff(['-i', source['path'], '-map', '0:a:0', '-vn', '-af', ','.join(af), '-ar', d['audio_sample_rate'],
                    '-ac', d['audio_channels'], '-c:a', 'pcm_f32le', '-threads', '2', '-f', 'f32le', pcm])
                require(pcm.stat().st_size == samples * d['audio_channels'] * 4, 'Prepared audio sample count mismatch')
                audio_sha = digest(pcm)
            record = {'cache_key': cache_key, 'source_sha256': sha, 'video_sha256': digest(video),
                      'audio_sha256': audio_sha, 'retained_pixels_verified': True,
                      'source_frames': expected, 'frames': d['frames']}
            save(meta, record)
        require(digest(source['path']) == sha, f'Source changed while reading: {source["path"]}')
        records.append({**record, 'index': index, 'source_path': str(source['path']), 'label': source['label'],
                        'video_path': str(folder / 'video.nut'),
                        'pcm_path': str(folder / 'audio.f32le') if record['audio_sha256'] else None})
    selected = d['audio_source_index']
    if selected is not None:
        require(records[selected - 1]['pcm_path'], f'Selected audio source {selected} has no audio stream')
    return records


def layout_image(plan, path):
    """One static RGBA caption overlay; text never enters FFmpeg expressions."""
    d = plan.design
    image = Image.new('RGBA', plan.canvas, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if d['label_position'] != 'none' and any(s['label'] for s in plan.sources):
        try:
            font = ImageFont.truetype(d['font'] or 'DejaVuSans.ttf', d['font_size'])
        except OSError as exc:
            raise ManualError('No caption font found; set font to a local .ttf path, or disable labels') from exc
        for source, (x, y, w, h) in zip(plan.sources, plan.rects):
            if not source['label']:
                continue
            top, height = (0, y) if d['label_position'] == 'top_header' else (y + h, d['caption_height'])
            box = draw.multiline_textbbox((0, 0), source['label'], font=font, spacing=2)
            tw, th = box[2] - box[0], box[3] - box[1]
            require(tw <= w - 4 and th <= height - 4, f'Label does not fit its reserved strip: {source["label"]!r}; shorten it or change font/strip size')
            tx = {'left': x + 2, 'center': x + (w - tw) // 2, 'right': x + w - tw - 2}[d['label_align']]
            draw.multiline_text((tx - box[0], top + (height - th) // 2 - box[1]), source['label'],
                                font=font, fill=d['text_color'], spacing=2, align=d['label_align'])
    image.save(path)


def compose(plan, records, cache, work):
    d = plan.design
    overlay = work / 'labels.png'
    layout_image(plan, overlay)
    compkey = key({'version': COMPOSITION_VERSION, 'videos': [r['video_sha256'] for r in records],
                   'overlay': digest(overlay), 'design': {k: v for k, v in d.items()
                   if k not in ('loops', 'audio_source_index', 'font')},
                   'ffmpeg': run(['ffmpeg', '-version']).decode().splitlines()[0]})
    folder = cache / 'composition'
    folder.mkdir(parents=True, exist_ok=True)
    video, meta = folder / f'{compkey}.mp4', folder / f'{compkey}.json'
    if meta.exists():
        require(video.is_file() and digest(video) == json.loads(meta.read_text())['sha256'], f'Corrupt composition cache: {video}')
        return video, compkey
    inputs, filters = [], []
    for i, r in enumerate(records):
        inputs += ['-threads', '1', '-i', r['video_path']]
        filters.append(f'[{i}:v]setpts=PTS-STARTPTS,format=yuv420p,setsar=1[c{i}]')
    width, height = plan.canvas
    bg = d['background'].replace('#', '0x')
    if len(records) == 1:
        x, y, _, _ = plan.rects[0]
        filters.append(f'[c0]pad={width}:{height}:{x}:{y}:color={bg}[grid]')
    else:
        layout = '|'.join(f'{x}_{y}' for x, y, _, _ in plan.rects)
        filters.append(''.join(f'[c{i}]' for i in range(len(records))) +
                       f'xstack=inputs={len(records)}:layout={layout}:fill={bg},pad={width}:{height}:0:0:color={bg}[grid]')
    inputs += ['-threads', '1', '-i', overlay]
    filters.append(f'[grid][{len(records)}:v]overlay=0:0:format=yuv420:repeatlast=1[out]')
    ff([*inputs, '-filter_complex', ';'.join(filters), '-map', '[out]', '-an', '-frames:v', d['frames'],
        '-r', d['fps'], '-c:v', 'libx264', '-preset', d['preset'], '-crf', d['crf'], '-pix_fmt', 'yuv420p',
        '-threads', '2', '-video_track_timescale', Fraction(d['fps']).numerator, video])
    check_video(video, d, d['frames'], plan.canvas)
    save(meta, {'sha256': digest(video)})
    return video, compkey


def validate_output_paths(plan, prefix, paths):
    protected = {s['path'].resolve() for s in plan.sources} | {p.resolve() for p in plan.inputs}
    for path in paths:
        path = Path(path)
        require(path.resolve() not in protected and
                not (path.exists() and any(path.samefile(source) for source in protected)),
                f'Output would overwrite a source/configuration file: {path}')


def publish_file(staged, destination, plan):
    """Replace a directory entry, never truncate an existing shared inode."""
    destination = Path(destination)
    # Stage on the destination filesystem so os.replace is atomic even when
    # scratch and outputs are on different mounts. Clean up on copy/rename errors.
    with tempfile.NamedTemporaryFile(prefix=f'.{destination.name}.', suffix='.tmp',
                                     dir=destination.parent, delete=False) as temp:
        temporary = Path(temp.name)
    try:
        shutil.copyfile(staged, temporary)
        validate_output_paths(plan, None, [destination])
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def render(plan, cache, stage, prefix=None, overwrite=False):
    cache = Path(cache).expanduser().resolve()
    paths = []
    if stage != 'prepare':
        require(prefix is not None, '--output-prefix is required for all/compose')
        prefix = Path(prefix).expanduser().resolve()
        paths = [Path(f'{prefix}-{loops}x.mp4') for loops in plan.design['loops']]
        paths += [Path(f'{prefix}.preview.png'), Path(f'{prefix}.verification.json')]
        validate_output_paths(plan, prefix, paths)
        require(not any(cache == p or cache in p.parents for p in paths), 'Outputs must be outside the cache root')
        require(overwrite or not any(p.exists() for p in paths), 'Output already exists; choose a new prefix or pass --overwrite')
    records = prepare(plan, cache, stage)
    if stage == 'prepare':
        return {'stage': stage, 'source_count': len(records), 'prepared': records}
    prefix.parent.mkdir(parents=True, exist_ok=True)
    d = plan.design
    outputs = []
    with tempfile.TemporaryDirectory(prefix='comfy-manual-') as td:
        work = Path(td)
        publications = []
        grid, compkey = compose(plan, records, cache, work)
        base_hashes = frame_hashes(grid)
        for loops, output in zip(d['loops'], paths):
            args = ['-stream_loop', loops - 1, '-threads', '1', '-i', grid]
            selected = d['audio_source_index']
            if selected is not None:
                pcm = Path(records[selected - 1]['pcm_path'])
                repeated = work / 'repeated.f32le'
                with repeated.open('wb') as dst:
                    for _ in range(loops):
                        with pcm.open('rb') as src:
                            shutil.copyfileobj(src, dst)
                args += ['-f', 'f32le', '-ar', d['audio_sample_rate'], '-ac', d['audio_channels'], '-i', repeated]
            args += ['-map', '0:v:0', '-c:v', 'copy']
            if selected is not None:
                args += ['-map', '1:a:0', '-c:a', 'aac', '-b:a', '192k', '-threads:a', '2']
            else:
                args += ['-an']
            staged = work / f'final-{loops}x.mp4'
            args += ['-video_track_timescale', Fraction(d['fps']).numerator, '-movie_timescale', d['audio_sample_rate'],
                     '-movflags', '+faststart', staged]
            ff(args)
            info = check_video(staged, d, d['frames'] * loops, plan.canvas)
            video_stream = next(s for s in info['streams'] if s['codec_type'] == 'video')
            require(Fraction(video_stream.get('start_time', '0')) == 0 and
                    Fraction(video_stream['duration_ts']) * Fraction(video_stream['time_base']) == plan.duration * loops,
                    'Final video start/duration mismatch')
            require(frame_hashes(staged) == base_hashes * loops, 'Repeated video frame hashes do not match base grid')
            audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
            if selected is not None:
                require(audio is not None and Fraction(audio['duration_ts']) * Fraction(audio['time_base']) == plan.duration * loops,
                        'Audio presentation duration mismatch')
            ff(['-xerror', '-i', staged, '-threads', '2', '-f', 'null', '-'])
            publications.append((staged, output))
            outputs.append({'path': str(output), 'sha256': digest(staged), 'frames': d['frames'] * loops,
                            'duration': str(plan.duration * loops), 'audio': selected is not None})
        preview = work / 'preview.png'
        ff(['-i', publications[0][0], '-vf', f'select=eq(n\\,{d["frames"]//2})', '-frames:v', '1', '-threads', '2', preview])
        require(all(digest(s['path']) == r['source_sha256'] for s, r in zip(plan.sources, records)), 'A source changed during rendering')
        report = {'mode': 'manual', 'source_count': len(records), 'canvas': plan.canvas, 'fps': d['fps'],
                  'design': d, 'sources': records, 'video_rects': plan.rects, 'outputs': outputs,
                  'preview': str(paths[-2]), 'composition_key': compkey,
                  'original_hashes_unchanged': True, 'repeat_frame_hashes_verified': True,
                  'notes': ['Whole-grid repeats; no crossfade or claim of perceptually seamless source endpoints.',
                            'PCM is repeated before one continuous AAC encode; raw AAC decoding may expose tail padding.']}
        verification = work / 'verification.json'
        save(verification, report)
        publications += [(preview, paths[-2]), (verification, paths[-1])]
        validate_output_paths(plan, prefix, paths)
        for staged, destination in publications:
            publish_file(staged, destination, plan)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manual mode: ordered selected videos, no invented parameter axes.')
    parser.add_argument('--sources', required=True, help='JSON ordered sources; paths relative to this manifest')
    parser.add_argument('--design', required=True, help='JSON layout and native video assertions')
    parser.add_argument('--stage', choices=('prepare', 'compose', 'all'), default='all')
    parser.add_argument('--output-prefix', help='Write PREFIX-Nx.mp4, PREFIX.preview.png and PREFIX.verification.json')
    parser.add_argument('--cache-root', default=str(Path(tempfile.gettempdir()) / f'comfy-matrix-cache-{getattr(os, "getuid", lambda: "user")()}'))
    parser.add_argument('--overwrite', action='store_true', help='Replace final artifacts (never sources/configs)')
    parser.add_argument('--dry-run', action='store_true', help='Validate JSON, paths and layout, without probing media or writing files')
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.sources, args.design)
        if args.dry_run:
            result = {'mode': 'manual', 'source_count': len(plan.sources), 'canvas': plan.canvas,
                      'duration': str(plan.duration), 'fps': plan.design['fps'], 'video_rects': plan.rects,
                      'sources': [{**s, 'path': str(s['path'])} for s in plan.sources], 'design': plan.design}
        else:
            for tool in ('ffmpeg', 'ffprobe'):
                require(shutil.which(tool), f'{tool} is required on PATH')
            result = render(plan, args.cache_root, args.stage, args.output_prefix, args.overwrite)
        print(json.dumps(result, indent=2))
        return 0
    except (ManualError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
