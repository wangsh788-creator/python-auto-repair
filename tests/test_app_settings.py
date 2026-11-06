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


if __name__ == "__main__":
    unittest.main()
