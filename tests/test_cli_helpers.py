import json
import tempfile
import unittest
from pathlib import Path

from comfyui_matrix_videos.cli import (
    apply_filters,
    build_plan,
    first_video_path,
    get_first,
    load_manifest,
    select_values,
)


class CliHelpersTest(unittest.TestCase):
    def test_select_values_expands_lists(self):
        record = {"outputs": [{"file": "a.mp4"}, {"file": "b.mp4"}]}
        self.assertEqual(select_values(record, "outputs[].file"), ["a.mp4", "b.mp4"])

    def test_first_video_path_prefers_common_output_shape(self):
        record = {"outputs": [{"file": "image.png"}, {"file": "movie.mp4"}]}
        self.assertEqual(first_video_path(record, "outputs[].file"), "movie.mp4")

    def test_loads_json_results_object(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "batch.json"
            p.write_text(json.dumps({"results": [{"status": "success", "index": 0}]}))
            self.assertEqual(load_manifest(p), [{"status": "success", "index": 0}])

    def test_filters_and_plan(self):
        records = [
            {"status": "success", "params": {"phase": "a", "input_index": 1, "profile": "x"}},
            {"status": "success", "params": {"phase": "a", "input_index": 1, "profile": "y"}},
            {"status": "success", "params": {"phase": "b", "input_index": 1, "profile": "z"}},
        ]
        filtered = apply_filters(records, [("params.phase", "a")])
        plan = build_plan(filtered, row_key="params.input_index", col_key="params.profile")
        self.assertEqual(plan.rows, [1])
        self.assertEqual(plan.cols, ["x", "y"])
        self.assertEqual(len(plan.cell_records), 2)
        self.assertEqual(get_first(filtered[0], "params.profile"), "x")


if __name__ == "__main__":
    unittest.main()
