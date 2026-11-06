import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core


class ProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "source"
        self.folder.mkdir()
        self.state = self.root / "state"
        self.output = self.root / "desktop"
        core.configure_folder(self.folder, self.state)
        self.events = []

    def scan(self, analyzer, opener, progress=None):
        with patch.object(core.time, "sleep"):
            return core.run_scan(
                self.state, analyzer=analyzer, output_dir=self.output,
                opener=opener, progress=progress if progress is not None else self.events.append,
            )

    def test_progress_tracks_each_file_and_saved_file_exists_before_editor_open(self):
        for filename in ("a.py", "b.py"):
            (self.folder / filename).write_text("value = 1 / 0\n", encoding="utf-8")

        def opener(path):
            self.assertEqual(path.read_text(encoding="utf-8"), "value = 0\n")
            self.assertEqual(self.events[-1]["stage"], "opening")
            self.assertEqual(self.events[-2]["stage"], "saved")

        result = self.scan(lambda *_: {"has_errors": True, "corrected_code": "value = 0\n"}, opener)
        self.assertEqual(result, {"detected": 2, "checked": 2, "fixed": 2, "failed": 0, "errors": []})
        self.assertEqual(
            [(event["stage"], event["filename"], event["index"], event["total"]) for event in self.events],
            [
                ("scanning", "", 0, 0),
                ("analyzing", "a.py", 1, 2), ("saved", "a.py", 1, 2), ("opening", "a.py", 1, 2),
                ("analyzing", "b.py", 2, 2), ("saved", "b.py", 2, 2), ("opening", "b.py", 2, 2),
                ("done", "", 2, 2),
            ],
        )

    def test_pending_open_progress_does_not_reanalyze_file(self):
        (self.folder / "repair.py").write_text("value = 1 / 0\n", encoding="utf-8")

        def failed_open(_):
            raise OSError("editor unavailable")

        self.scan(lambda *_: {"has_errors": True, "corrected_code": "value = 0\n"}, failed_open)
        self.events.clear()

        def unexpected_analysis(*_):
            self.fail("An already saved repair was analyzed again")

        result = self.scan(unexpected_analysis, lambda _: None)
        self.assertEqual(result["detected"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual([event["stage"] for event in self.events], ["scanning", "opening", "done"])
        self.assertEqual(self.events[1], {"stage": "opening", "filename": "repair.py", "index": 1, "total": 1})

    def test_progress_callback_failure_does_not_prevent_repair(self):
        (self.folder / "repair.py").write_text("value = 1 / 0\n", encoding="utf-8")

        def broken_callback(_):
            raise RuntimeError("window has closed")

        result = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": "value = 0\n"}, lambda _: None,
            progress=broken_callback,
        )
        self.assertEqual(result["fixed"], 1)
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()
