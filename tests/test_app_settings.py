import importlib.machinery
import importlib.util
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
app_path = Path(__file__).resolve().parents[1] / "app.pyw"
loader = importlib.machinery.SourceFileLoader("repair_app_settings", str(app_path))
spec = importlib.util.spec_from_loader(loader.name, loader)
app = importlib.util.module_from_spec(spec)
loader.exec_module(app)


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.window = app.SettingsWindow.__new__(app.SettingsWindow)
        self.window.state_dir = Path(self.temporary.name)
        self.window.root = Mock()
        self.window.busy = False
        self.window.exiting = False
        self.window.choosing_folder = False
        self.window.visible = True
        self.window.close_when_done = False
        self.window.automatic_scan = False
        self.window.activity = None
        self.window.scan_idle_status = "就绪"
        self.window.started_at = None
        self.window.monitor = Mock()
        self.window.exit_button = Mock()
        self.window.redact_values = ()
        self.window.mode = Value(app.MODE_LABELS["custom_api"])
        self.window.api_format = Value(app.FORMAT_LABELS["chat_completions"])
        self.window.base_url = Value("https://gateway.example/v1")
        self.window.model = Value("test-model")
        self.window.api_key = Value("")
        self.window.status = Value()
        self.window.folder = Value(str(self.window.state_dir))
        self.window.mode_changed = Mock()
        self.window.refresh_connection_status = Mock()
        self.window.events = queue.Queue()

    def test_blank_key_preserves_saved_credentials_without_reading_them_back(self):
        saved = {"mode": "custom_api", "has_api_key": True}
        with patch.object(app, "save_connection", return_value=saved) as save:
            self.assertTrue(self.window.save_current_connection())
        self.assertIsNone(save.call_args.kwargs["api_key"])
        self.assertEqual(save.call_args.args[1], {
            "mode": "custom_api", "base_url": "https://gateway.example/v1",
            "model": "test-model", "api_format": "chat_completions",
        })
        self.assertEqual(self.window.api_key.get(), "")
        self.assertEqual(self.window.saved_connection, saved)

    def test_failed_save_does_not_expose_entered_key_in_status_dialog_or_log(self):
        secret = "test-private-credential"
        self.window.api_key.set(secret)
        with patch.object(app, "save_connection", side_effect=ValueError("invalid " + secret)):
            with patch.object(app, "log_error") as log:
                with patch.object(app.messagebox, "showerror") as dialog:
                    self.assertFalse(self.window.save_current_connection())
        self.assertNotIn(secret, self.window.status.get())
        self.assertNotIn(secret, log.call_args.args[1])
        self.assertNotIn(secret, dialog.call_args.args[1])
        self.assertEqual(self.window.api_key.get(), secret)

    def test_api_scan_uses_backend_readiness_without_requiring_codex(self):
        self.window.save_current_connection = Mock(return_value=True)
        self.window.set_busy = Mock()
        self.window.render_progress = Mock()
        with patch.object(app, "ensure_connection") as ready:
            with patch.object(app, "find_codex", side_effect=AssertionError("API mode needs no CLI")) as cli:
                with patch.object(app.threading, "Thread") as thread:
                    self.window.scan()
                    ready.assert_not_called()
                    with patch.object(app, "run_scan", return_value={"failed": 0}):
                        self.window.worker()
                ready.assert_called_once_with(self.window.state_dir, cli_resolver=cli)
        cli.assert_not_called()
        thread.return_value.start.assert_called_once()

    def test_connection_failure_uses_safe_background_result(self):
        secret = "test-private-credential"
        self.window.redact_values = (secret,)
        with patch.object(app, "check_connection", side_effect=RuntimeError("rejected " + secret)):
            with patch.object(app, "log_error") as log:
                self.window.connection_worker("test")
        kind, detail = self.window.events.get_nowait()
        self.assertEqual(kind, "connection_error")
        self.assertNotIn(secret, detail)
        self.assertNotIn(secret, log.call_args.args[1])

    def test_first_launch_opens_full_setup_instead_of_scanning(self):
        with patch.object(app, "RunLock"):
            with patch.object(app, "read_state", return_value={}):
                with patch.object(app, "SettingsWindow") as settings:
                    with patch.object(app, "run_scan") as scan:
                        result = app.main(["--state-dir", str(self.window.state_dir)])
        self.assertEqual(result, 0)
        settings.assert_called_once_with(self.window.state_dir.resolve())
        settings.return_value.run.assert_called_once()
        scan.assert_not_called()

    def test_existing_folder_starts_resident_window_hidden(self):
        with patch.object(app, "RunLock"):
            with patch.object(app, "read_state", return_value={"folder": str(self.window.state_dir)}):
                with patch.object(app, "SettingsWindow") as settings:
                    with patch.object(app, "run_scan") as scan:
                        result = app.main(["--state-dir", str(self.window.state_dir)])
        self.assertEqual(result, 0)
        settings.return_value.close.assert_called_once()
        settings.return_value.run.assert_called_once()
        scan.assert_not_called()

    def test_headless_once_keeps_single_scan_without_window(self):
        with patch.object(app, "RunLock"):
            with patch.object(app, "read_state", return_value={"folder": str(self.window.state_dir)}):
                with patch.object(app, "SettingsWindow") as settings:
                    with patch.object(app, "run_scan", return_value={"failed": 0}) as scan:
                        result = app.main(["--headless", "--once", "--state-dir", str(self.window.state_dir)])
        self.assertEqual(result, 0)
        settings.assert_not_called()
        scan.assert_called_once_with(self.window.state_dir.resolve())

    def test_duplicate_launch_requests_existing_resident_settings(self):
        lock = Mock()
        lock.__enter__ = Mock(side_effect=app.AlreadyRunning())
        lock.__exit__ = Mock(return_value=False)
        with patch.object(app, "RunLock", return_value=lock):
            with patch.object(app, "request_settings") as request:
                with patch.object(app, "SettingsWindow") as settings:
                    result = app.main(["--settings", "--state-dir", str(self.window.state_dir)])
        self.assertEqual(result, 0)
        request.assert_called_once_with(self.window.state_dir.resolve())
        settings.assert_not_called()

    def test_closing_busy_window_keeps_background_work_alive(self):
        self.window.busy = True
        self.window.close()
        self.window.root.withdraw.assert_called_once()
        self.window.root.destroy.assert_not_called()
        self.assertFalse(self.window.visible)
        self.assertFalse(self.window.close_when_done)

    def test_explicit_exit_waits_for_current_task_then_destroys_root(self):
        self.window.busy = True
        self.window.activity = "scan"
        self.window.set_busy = Mock()
        self.window.exit_background()
        self.window.root.destroy.assert_not_called()
        self.assertTrue(self.window.exiting)
        self.assertTrue(self.window.close_when_done)
        self.window.finish("result", {"detected": 1, "checked": 1, "fixed": 1, "failed": 0})
        self.window.root.destroy.assert_called_once()
        self.window.monitor.finish.assert_called_once_with(failed=False)

    def test_automatic_errors_update_status_without_popup(self):
        self.window.activity = "scan"
        self.window.automatic_scan = True
        self.window.set_busy = Mock()
        with patch.object(app.messagebox, "showerror") as dialog:
            self.window.finish("error", "offline")
        self.assertIn("offline", self.window.status.get())
        self.window.monitor.finish.assert_called_once_with(failed=True)
        dialog.assert_not_called()

    def test_idle_probe_does_not_cancel_failure_backoff(self):
        self.window.activity = "scan"
        self.window.automatic_scan = True
        self.window.set_busy = Mock()
        self.window.finish("monitor_idle", None)
        self.window.monitor.finish.assert_not_called()
        self.assertEqual(self.window.status.get(), "就绪")

    def test_monitor_tick_starts_only_when_idle_and_due(self):
        self.window.monitor.due.return_value = True
        self.window.begin_scan = Mock()
        self.window.monitor_tick()
        self.window.begin_scan.assert_called_once_with(automatic=True)
        self.window.root.after.assert_called_once_with(250, self.window.monitor_tick)
        self.window.begin_scan.reset_mock()
        self.window.busy = True
        self.window.monitor_tick()
        self.window.begin_scan.assert_not_called()

    def test_folder_selection_pauses_background_monitor_until_dialog_closes(self):
        self.window.begin_scan = Mock()
        self.window.monitor.due.return_value = True

        def select_folder(**_):
            self.assertTrue(self.window.choosing_folder)
            self.window.monitor_tick()
            self.window.begin_scan.assert_not_called()
            return ""

        with patch.object(app.filedialog, "askdirectory", side_effect=select_folder):
            self.window.choose()
        self.assertFalse(self.window.choosing_folder)


if __name__ == "__main__":
    unittest.main()
