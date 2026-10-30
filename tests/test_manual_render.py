"""Tiny CPU-only FFmpeg regression tests; skipped only when binaries are absent."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from comfyui_matrix_videos import manual


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/ffprobe required')
class ManualRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='matrix-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / 'cache'
        self.manifest = {'sources': [{'path': 'red.mp4', 'label': 'R'}, {'path': 'blue.mp4', 'label': 'B'}]}
        self.design = {'rows': 1, 'columns': 2, 'cell_width': 64, 'cell_height': 48, 'fps': '12',
                       'frames': 6, 'gutter': 4, 'caption_height': 20, 'font_size': 12, 'margin': 4,
                       'audio_source_index': 1, 'loops': [1, 2], 'preset': 'ultrafast'}
        for color in ('red', 'blue'):
            args = ['-f', 'lavfi', '-i', f'color={color}:s=64x48:r=12:d=0.5']
            if color == 'red':
                args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=0.5']
            manual.ff([*args, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-threads', '2',
                       '-c:a', 'aac', '-threads:a', '2', self.root / f'{color}.mp4'])

    def plan(self):
        sources, design = self.root / 'sources.json', self.root / 'design.json'
        manual.save(sources, self.manifest)
        manual.save(design, self.design)
        return manual.load_plan(sources, design)

    def test_stages_caches_audio_order_loop_and_source_preservation(self):
        plan = self.plan()
        before = [manual.digest(s['path']) for s in plan.sources]
        with self.assertRaisesRegex(manual.ManualError, 'cache missing'):
            manual.render(plan, self.cache, 'compose', self.root / 'missing')
        prepared = manual.render(plan, self.cache, 'prepare')['prepared']
        mtimes = [Path(r['video_path']).stat().st_mtime_ns for r in prepared]
        report = manual.render(plan, self.cache, 'compose', self.root / 'grid')
        self.assertEqual(report['canvas'], (140, 76))
        self.assertEqual([o['frames'] for o in report['outputs']], [6, 12])
        self.assertEqual([o['duration'] for o in report['outputs']], ['1/2', '1'])
        self.assertTrue(report['repeat_frame_hashes_verified'])
        self.assertEqual(before, [manual.digest(s['path']) for s in plan.sources])
        self.assertEqual(mtimes, [Path(r['video_path']).stat().st_mtime_ns for r in prepared])
        from PIL import Image
        image = Image.open(report['preview']).convert('RGB')
        red, blue = image.getpixel((20, 20)), image.getpixel((90, 20))
        self.assertGreater(red[0], 200)
        self.assertGreater(blue[2], 200)
        self.assertLess(red[2], 30)
        # Loop/audio changes reuse composed video, and all prepares remain reusable.
        self.design.update(audio_source_index=None, loops=[3])
        silent = manual.render(self.plan(), self.cache, 'compose', self.root / 'silent')
        self.assertEqual(report['composition_key'], silent['composition_key'])
        self.assertFalse(silent['outputs'][0]['audio'])
        # Labels/order edit only changes composition; source preparation keys are stable.
        self.manifest['sources'].reverse()
        self.manifest['sources'][0]['label'] = 'New'
        reordered = manual.render(self.plan(), self.cache, 'compose', self.root / 'reordered')
        self.assertNotEqual(report['composition_key'], reordered['composition_key'])
        self.assertEqual(set(r['cache_key'] for r in prepared), set(r['cache_key'] for r in reordered['sources']))
        with self.assertRaisesRegex(manual.ManualError, 'already exists'):
            manual.render(self.plan(), self.cache, 'all', self.root / 'reordered')
        Path(prepared[0]['video_path']).write_bytes(b'corrupt cache')
        with self.assertRaisesRegex(manual.ManualError, 'Corrupt preparation'):
            manual.render(self.plan(), self.cache, 'prepare')

    def test_one_cell_explicit_trim_fade_and_missing_audio(self):
        self.manifest['sources'] = self.manifest['sources'][:1]
        self.manifest['sources'][0]['preparation'] = {'expected_source_frames': 6, 'end_fade_ms': 20}
        self.design.update(columns=1, frames=5, label_position='top_header', caption_height=0, margin=24, loops=[1])
        report = manual.render(self.plan(), self.cache, 'all', self.root / 'single')
        self.assertEqual(report['outputs'][0]['frames'], 5)
        self.assertTrue(report['sources'][0]['retained_pixels_verified'])
        self.manifest['sources'][0]['path'] = 'blue.mp4'
        with self.assertRaisesRegex(manual.ManualError, 'no audio'):
            manual.render(self.plan(), self.cache, 'prepare')

    def test_mismatch_and_missing_trim_rejected(self):
        self.design['frames'] = 5
        with self.assertRaisesRegex(manual.ManualError, 'Frame count mismatch'):
            manual.render(self.plan(), self.cache, 'prepare')
        self.design.update(frames=6, cell_width=66)
        with self.assertRaisesRegex(manual.ManualError, 'dimensions mismatch'):
            manual.render(self.plan(), self.cache, 'prepare')

    def test_hardlinked_output_rejected_without_changing_source(self):
        plan = self.plan()
        source = plan.sources[0]['path']
        before = source.read_bytes()
        output = self.root / 'hardlink-1x.mp4'
        os.link(source, output)
        # Check preservation even when an unexpected exception is raised.
        try:
            with self.assertRaisesRegex(manual.ManualError, 'overwrite a source/configuration'):
                manual.render(plan, self.cache, 'all', self.root / 'hardlink', overwrite=True)
        finally:
            self.assertEqual(source.read_bytes(), before, 'Original source was modified')
        self.assertFalse(self.cache.exists(), 'Collision must fail before preparation writes')

    def test_overwrite_replaces_each_artifact_not_its_shared_inode(self):
        self.design['loops'] = [1, 2]
        plan = self.plan()
        prefix = self.root / 'replace'
        suffixes = ('-1x.mp4', '-2x.mp4', '.preview.png', '.verification.json')
        aliases = []
        for index, suffix in enumerate(suffixes):
            sentinel = self.root / f'old-artifact-{index}'
            sentinel.write_bytes(b'prior artifact bytes')
            target = Path(f'{prefix}{suffix}')
            os.link(sentinel, target)
            aliases.append((sentinel, target))
        report = manual.render(plan, self.cache, 'all', prefix, overwrite=True)
        for sentinel, target in aliases:
            with self.subTest(target=target.name):
                self.assertEqual(sentinel.read_bytes(), b'prior artifact bytes')
                self.assertFalse(target.samefile(sentinel))
        self.assertEqual(json.loads(aliases[-1][1].read_text())['outputs'], report['outputs'])
        for output in report['outputs']:
            self.assertEqual(manual.digest(output['path']), output['sha256'])

    def test_warm_cache_rejects_changed_native_width(self):
        self.check_warm_cache_geometry('cell_width', 66)

    def test_warm_cache_rejects_changed_native_height(self):
        self.check_warm_cache_geometry('cell_height', 50)

    def check_warm_cache_geometry(self, field, value):
        self.manifest['sources'] = self.manifest['sources'][:1]
        self.design.update(columns=1, loops=[1])
        prepared = manual.render(self.plan(), self.cache, 'prepare')['prepared']
        mtimes = [Path(r['video_path']).stat().st_mtime_ns for r in prepared]
        self.design[field] = value
        changed = self.plan()
        for stage in ('compose', 'all', 'prepare'):
            with self.subTest(stage=stage):
                prefix = self.root / f'wrong-{stage}'
                with self.assertRaisesRegex(manual.ManualError, 'cache missing|dimensions mismatch'):
                    manual.render(changed, self.cache, stage, prefix)
                self.assertEqual(list(self.root.glob(f'{prefix.name}*')), [])
        self.assertEqual(mtimes, [Path(r['video_path']).stat().st_mtime_ns for r in prepared])

    def test_label_overflow_rejected(self):
        self.manifest['sources'][0]['label'] = 'far too long to fit this cell'
        with self.assertRaisesRegex(manual.ManualError, 'does not fit'):
            manual.layout_image(self.plan(), self.root / 'labels.png')


if __name__ == '__main__':
    unittest.main()
