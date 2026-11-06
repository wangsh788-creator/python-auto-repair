from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core
from monitor import FolderMonitor, consume_settings_request, folder_snapshot, request_settings


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state_dir = self.root / "state"
        self.folder = self.root / "source"
        self.folder.mkdir()
        self.clock = Clock()

    def test_snapshot_tracks_python_changes_and_ignores_exports_and_dependencies(self):
        core.configure_folder(self.folder, self.state_dir)
        source = self.folder / "sub" / "test.PY"
        source.parent.mkdir()
        source.write_text("value = 1\n", encoding="utf-8")
        dependency = self.folder / ".venv" / "ignored.py"
        dependency.parent.mkdir()
        dependency.write_text("value = 1\n", encoding="utf-8")
        exported = self.folder / "generated.py"
        exported.write_text("value = 2\n", encoding="utf-8")
        state = core.read_state(self.state_dir)
        state["exports"] = [str(exported)]
        core.write_state(self.state_dir, state)
        before = folder_snapshot(self.state_dir)
        self.assertEqual([entry[0] for entry in before[1]], ["sub/test.PY"])
        source.write_text("value = 12345\n", encoding="utf-8")
        self.assertNotEqual(folder_snapshot(self.state_dir), before)
        source.unlink()
        self.assertEqual(folder_snapshot(self.state_dir)[1], ())

    def test_missing_configuration_or_folder_waits_without_scan(self):
        self.assertIsNone(folder_snapshot(self.state_dir))
        core.configure_folder(self.folder, self.state_dir)
        self.folder.rmdir()
        monitor = FolderMonitor(self.state_dir, clock=self.clock)
        self.assertFalse(monitor.begin())
        self.assertFalse(monitor.active)

    def test_unchanged_files_do_not_start_another_scan(self):
        snapshot = Mock(return_value=("folder", "initial"))
        monitor = FolderMonitor(self.state_dir, clock=self.clock, snapshot=snapshot)
        self.assertTrue(monitor.begin())
        monitor.finish()
        self.assertFalse(monitor.begin())
        self.clock.now = 2
        self.assertFalse(monitor.begin())
        snapshot.return_value = ("folder", "changed")
        self.clock.now = 4
        self.assertTrue(monitor.begin())

    def test_files_arriving_during_analysis_are_handled_after_current_scan(self):
        snapshot = Mock(return_value=("folder", "first"))
        monitor = FolderMonitor(self.state_dir, clock=self.clock, snapshot=snapshot)
        self.assertTrue(monitor.begin())
        snapshot.return_value = ("folder", "second")
        self.clock.now = 5
        self.assertFalse(monitor.begin())
        monitor.finish()
        self.clock.now = 7
        self.assertTrue(monitor.begin())
        self.assertEqual(monitor.active_snapshot, ("folder", "second"))

    def test_failures_wait_sixty_seconds_then_retry_unchanged_source(self):
        monitor = FolderMonitor(self.state_dir, clock=self.clock, snapshot=lambda _: "same")
        self.assertTrue(monitor.begin())
        monitor.finish(failed=True)
        self.clock.now = 59
        self.assertFalse(monitor.due())
        self.assertFalse(monitor.begin())
        self.clock.now = 60
        self.assertTrue(monitor.begin())
        monitor.finish()
        self.clock.now = 62
        self.assertFalse(monitor.begin())

    def test_manual_scan_and_saved_configuration_can_override_backoff(self):
        monitor = FolderMonitor(self.state_dir, clock=self.clock, snapshot=lambda _: "same")
        self.assertTrue(monitor.begin())
        monitor.finish(failed=True)
        self.assertTrue(monitor.begin(automatic=False))
        monitor.finish(failed=True)
        monitor.invalidate()
        self.assertTrue(monitor.begin())

    def test_snapshot_is_reserved_before_another_thread_can_begin(self):
        entered = threading.Event()
        released = threading.Event()

        def snapshot(_):
            entered.set()
            self.assertTrue(released.wait(timeout=2))
            return "snapshot"

        monitor = FolderMonitor(self.state_dir, clock=self.clock, snapshot=snapshot)
        results = []
        worker = threading.Thread(target=lambda: results.append(monitor.begin()))
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.assertFalse(monitor.begin(automatic=False))
        finally:
            released.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True])

    def test_snapshot_failure_releases_reservation_for_later_retry(self):
        snapshot = Mock(side_effect=[OSError("unavailable"), "ready"])
        monitor = FolderMonitor(self.state_dir, clock=self.clock, snapshot=snapshot)
        with self.assertRaises(OSError):
            monitor.begin()
        self.assertFalse(monitor.active)
        monitor.finish(failed=True)
        self.clock.now = 60
        self.assertTrue(monitor.begin())

    def test_repeated_settings_requests_coalesce_and_are_consumed_once(self):
        request_settings(self.state_dir)
        request_settings(self.state_dir)
        self.assertTrue(consume_settings_request(self.state_dir))
        self.assertFalse(consume_settings_request(self.state_dir))
        self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_polling_repairs_new_and_changed_same_name_without_repeating_analysis(self):
        core.configure_folder(self.folder, self.state_dir)
        output_dir = self.root / "results"
        output_dir.mkdir()
        monitor = FolderMonitor(self.state_dir, clock=self.clock)
        calls = []
        opened = []

        def analyzer(source, filename):
            calls.append(filename)
            return {"has_errors": True, "corrected_code": source.replace("/ 0", "/ 1")}

        def tick():
            if not monitor.begin():
                return None
            result = core.run_scan(self.state_dir, analyzer=analyzer, output_dir=output_dir, opener=opened.append)
            monitor.finish(failed=bool(result["failed"]))
            return result

        self.assertEqual(tick()["checked"], 0)
        source = self.folder / "example.py"
        source.write_text("value = 6 / 0\n", encoding="utf-8")
        self.clock.now = 2
        self.assertEqual(tick()["fixed"], 1)
        self.clock.now = 4
        self.assertIsNone(tick())
        source.write_text("value = 123 / 0\n", encoding="utf-8")
        self.clock.now = 6
        self.assertEqual(tick()["fixed"], 1)
        self.assertEqual(calls, ["example.py", "example.py"])
        self.assertEqual([path.name for path in opened], ["example.py", "example.py"])
        self.assertEqual(source.read_text(encoding="utf-8"), "value = 123 / 0\n")
        self.assertTrue(all(path.is_file() for path in opened))


if __name__ == "__main__":
    unittest.main()
