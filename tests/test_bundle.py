import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BundleTest(unittest.TestCase):
    def test_bundles_match(self):
        subprocess.run([sys.executable, str(ROOT / 'scripts/sync_standalone.py'), '--check'], check=True, capture_output=True)

    def test_installed_module_and_copyable_skill_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            copied = Path(td) / 'skill'
            shutil.copytree(ROOT / 'skills/creative/comfyui-matrix-videos', copied)
            for command in ([sys.executable, '-m', 'comfyui_matrix_videos.cli'],
                            [sys.executable, str(copied / 'scripts/comfy_matrix_video.py')]):
                for mode in ('manual', 'sweep'):
                    result = subprocess.run([*command, mode, '--help'], cwd=td, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                # Existing flag-only sweep remains identical to explicit sweep.
                args = ['--manifest', str(ROOT / 'examples/comfy_batch_manifest.jsonl'),
                        '--row-key', 'args.steps', '--col-key', 'args.seed', '--dry-run']
                old = subprocess.run([*command, *args], cwd=td, text=True, capture_output=True, check=True)
                explicit = subprocess.run([*command, 'sweep', *args], cwd=td, text=True, capture_output=True, check=True)
                self.assertEqual(json.loads(old.stdout), json.loads(explicit.stdout))


if __name__ == '__main__':
    unittest.main()
