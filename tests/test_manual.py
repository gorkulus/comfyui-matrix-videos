"""Contract tests for the ordered-selection mode (no media required)."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comfyui_matrix_videos import manual


class ManualPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = self.root / 'sources.json'
        self.design = self.root / 'design.json'
        self.manifest = {'sources': [{'path': 'b.mp4', 'label': 'B'}, {'path': 'a.mp4', 'label': 'A'}]}
        self.settings = {'rows': 1, 'columns': 2, 'cell_width': 64, 'cell_height': 48,
                         'fps': '24', 'frames': 12}
        for name in ('a.mp4', 'b.mp4'):
            (self.root / name).write_bytes(b'placeholder')

    def load(self):
        self.sources.write_text(json.dumps(self.manifest))
        self.design.write_text(json.dumps(self.settings))
        return manual.load_plan(self.sources, self.design)

    def test_order_relative_paths_and_safe_defaults(self):
        plan = self.load()
        self.assertEqual([s['path'].name for s in plan.sources], ['b.mp4', 'a.mp4'])
        self.assertEqual([s['label'] for s in plan.sources], ['B', 'A'])
        self.assertEqual(plan.sources[0]['preparation'], {})
        self.assertIsNone(plan.design['audio_source_index'])
        self.assertEqual(plan.design['loops'], [1])
        self.assertEqual(plan.duration, manual.Fraction(1, 2))

    def test_geometry(self):
        self.settings.update(gutter=4, caption_height=20, font_size=14, margins={'left': 2, 'right': 6, 'top': 8, 'bottom': 10})
        plan = self.load()
        self.assertEqual(plan.canvas, (140, 86))
        self.assertEqual(plan.rects, [(2, 8, 64, 48), (70, 8, 64, 48)])

    def test_count_and_invalid_values(self):
        for change in ({'rows': 2}, {'fps': '0'}, {'frames': -1}, {'gutter': 3},
                       {'cell_width': 63}, {'loops': [0]}, {'loops': [True]},
                       {'audio_source_index': 0}, {'audio_source_index': 3},
                       {'label_position': 'overlay'}, {'unknown': 1}, {'fps': 'nan'},
                       {'square': True, 'margins': {'left': 4}}):
            with self.subTest(change=change):
                old = copy.deepcopy(self.settings)
                self.settings.update(change)
                with self.assertRaises(manual.ManualError):
                    self.load()
                self.settings = old

    def test_header_labels_only_first_row(self):
        self.settings.update(rows=2, columns=1, label_position='top_header', caption_height=0, margin=40)
        with self.assertRaisesRegex(manual.ManualError, 'first row'):
            self.load()

    def test_missing_file_and_expected_count(self):
        self.manifest['expected_count'] = 3
        with self.assertRaises(manual.ManualError):
            self.load()
        del self.manifest['expected_count']
        (self.root / 'a.mp4').unlink()
        with self.assertRaisesRegex(manual.ManualError, 'not found'):
            self.load()

    def test_preparation_is_explicit(self):
        self.manifest['sources'][0]['preparation'] = {'expected_source_frames': 13, 'end_fade_ms': 15}
        self.assertEqual(self.load().sources[0]['preparation']['expected_source_frames'], 13)
        self.manifest['sources'][0]['preparation'] = {'end_fade_ms': -1}
        with self.assertRaises(manual.ManualError):
            self.load()

    def test_output_cannot_replace_source(self):
        plan = self.load()
        with self.assertRaisesRegex(manual.ManualError, 'source'):
            manual.validate_output_paths(plan, self.root / 'a', [self.root / 'a.mp4'])

    def test_all_final_paths_reject_source_config_and_font_aliases(self):
        font = self.root / 'font.ttf'
        font.write_bytes(b'disposable font fixture; preflight never renders it')
        self.settings.update(font='font.ttf', loops=[1, 2])
        plan = self.load()
        protected = [s['path'] for s in plan.sources] + plan.inputs
        before = {p: p.read_bytes() for p in protected}
        prefix = self.root / 'alias'
        for link_type in ('hardlink', 'symlink'):
            for suffix in ('-1x.mp4', '-2x.mp4', '.preview.png', '.verification.json'):
                target = Path(f'{prefix}{suffix}')
                for source in protected:
                    with self.subTest(link_type=link_type, suffix=suffix, source=source.name):
                        if link_type == 'hardlink':
                            target.hardlink_to(source)
                        else:
                            target.symlink_to(source)
                        try:
                            with patch.object(manual, 'prepare') as prepare:
                                with self.assertRaisesRegex(manual.ManualError, 'overwrite a source/configuration'):
                                    manual.render(plan, self.root / 'cache', 'all', prefix, overwrite=True)
                                prepare.assert_not_called()
                            self.assertEqual({p: p.read_bytes() for p in protected}, before)
                            self.assertFalse((self.root / 'cache').exists())
                        finally:
                            target.unlink()

    def test_publication_failure_preserves_old_file_and_cleans_temp(self):
        plan = self.load()
        staged, target = self.root / 'staged', self.root / 'final.mp4'
        staged.write_bytes(b'new content')
        target.write_bytes(b'old content')

        def fail_copy(source, destination):
            self.assertEqual(Path(destination).parent, target.parent)
            Path(destination).write_bytes(b'partial copy')
            raise OSError('injected copy failure')

        for operation in ('copy', 'replace'):
            with self.subTest(operation=operation):
                mock_target = 'shutil.copyfile' if operation == 'copy' else 'os.replace'
                effect = fail_copy if operation == 'copy' else OSError('injected rename failure')
                with patch.object(manual.shutil if operation == 'copy' else manual.os,
                                  mock_target.split('.')[-1], side_effect=effect):
                    with self.assertRaisesRegex(OSError, 'injected'):
                        manual.publish_file(staged, target, plan)
                self.assertEqual(target.read_bytes(), b'old content')
                self.assertEqual(list(self.root.glob('.final.mp4.*.tmp')), [])

    def test_publication_rechecks_protected_alias(self):
        plan = self.load()
        staged, target = self.root / 'staged', self.root / 'final.mp4'
        staged.write_bytes(b'new content')
        # A collision appearing after render preflight must still be rejected.
        manual.validate_output_paths(plan, None, [target])
        source = plan.sources[0]['path']
        before = source.read_bytes()
        target.hardlink_to(source)
        with self.assertRaisesRegex(manual.ManualError, 'overwrite a source/configuration'):
            manual.publish_file(staged, target, plan)
        self.assertEqual(source.read_bytes(), before)
        self.assertTrue(target.samefile(source))
        self.assertEqual(list(self.root.glob('.final.mp4.*.tmp')), [])

    def test_cache_keys_separate_design_order_and_labels(self):
        plan = self.load()
        first = manual.preparation_key('abc', plan.sources[0], plan.design, 'ffmpeg-test')
        plan.design['gutter'] = 20
        plan.sources[0]['label'] = 'renamed'
        self.assertEqual(first, manual.preparation_key('abc', plan.sources[0], plan.design, 'ffmpeg-test'))
        plan.sources[0]['preparation'] = {'end_fade_ms': 15}
        self.assertNotEqual(first, manual.preparation_key('abc', plan.sources[0], plan.design, 'ffmpeg-test'))

    def test_cli_dispatch_does_not_change_sweep(self):
        from comfyui_matrix_videos import cli
        with patch('comfyui_matrix_videos.manual.main', return_value=0) as main:
            self.assertEqual(cli.main(['manual', '--help']), 0)
            main.assert_called_once_with(['--help'])
        with patch.object(cli, 'build_parser') as parser:
            parser.return_value.parse_args.side_effect = RuntimeError('legacy parser')
            with self.assertRaisesRegex(RuntimeError, 'legacy parser'):
                cli.main(['--manifest', 'batch.json'])
            parser.return_value.parse_args.assert_called_once_with(['--manifest', 'batch.json'])


if __name__ == '__main__':
    unittest.main()
