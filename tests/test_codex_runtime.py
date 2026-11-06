import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.login_home = self.state_dir / "codex-home"

    def invoke_success(self, environment):
        process = Mock()
        process.returncode = 0
        process.communicate.return_value = (None, None)

        def spawn(command, **options):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps({"has_errors": False, "corrected_code": ""}), encoding="utf-8")
            return process

        with patch.dict(core.os.environ, environment, clear=True):
            with patch.object(core, "default_state_dir", return_value=self.state_dir):
                with patch.object(core, "find_codex", return_value=Path("codex.exe")):
                    with patch.object(core, "_configured_model", return_value=None):
                        with patch.object(core.subprocess, "Popen", side_effect=spawn) as popen:
                            result = core.analyze_with_codex("value = 1\n", "source.py")
                            inherited_environment = dict(core.os.environ)
        self.assertEqual(result, {"has_errors": False, "corrected_code": ""})
        return popen.call_args, inherited_environment

    def test_dedicated_login_overrides_global_auth_only_in_child(self):
        self.login_home.mkdir(parents=True)
        (self.login_home / "auth.json").write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
        environment = {
            "CODEX_HOME": str(Path(self.temporary.name) / "global-codex"),
            "OPENAI_API_KEY": "invalid-test-key",
            "CODEX_API_KEY": "invalid-test-codex-key",
            "CODEX_ACCESS_TOKEN": "invalid-test-token",
            "OPENAI_BASE_URL": "https://unused.invalid/v1",
            "OPENAI_ORG_ID": "org-test",
            "OPENAI_PROJECT_ID": "project-test",
            "CODEX_THREAD_ID": "parent-thread",
            "UNRELATED_SETTING": "retained",
        }
        arguments, unchanged = self.invoke_success(environment)
        child = arguments.kwargs["env"]
        self.assertEqual(child["CODEX_HOME"], str(self.login_home))
        self.assertEqual(child["UNRELATED_SETTING"], "retained")
        for key in environment.keys() - {"CODEX_HOME", "UNRELATED_SETTING"}:
            self.assertNotIn(key, child)
        self.assertIn('forced_login_method="chatgpt"', arguments.args[0])
        self.assertEqual(unchanged, environment)

    def test_missing_dedicated_auth_keeps_existing_cli_login(self):
        self.login_home.mkdir(parents=True)
        environment = {"CODEX_HOME": str(Path(self.temporary.name) / "global-codex")}
        arguments, unchanged = self.invoke_success(environment)
        self.assertEqual(arguments.kwargs["env"]["CODEX_HOME"], environment["CODEX_HOME"])
        self.assertNotIn('forced_login_method="chatgpt"', arguments.args[0])
        self.assertEqual(unchanged, environment)

    def test_explicit_auth_failure_stops_retries_at_first_poll(self):
        process = Mock()
        process.pid = 987654
        process.poll.return_value = None
        process.returncode = 1
        process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 1), (None, None)]

        def spawn(command, **options):
            options["stderr"].write(b"ERROR: unexpected HTTP status 401 Unauthorized: invalid_api_key\n")
            options["stderr"].flush()
            return process

        with patch.object(core, "default_state_dir", return_value=self.state_dir):
            with patch.object(core, "find_codex", return_value=Path("codex.exe")):
                with patch.object(core, "_configured_model", return_value=None):
                    with patch.object(core.subprocess, "Popen", side_effect=spawn):
                        with patch.object(core.subprocess, "run") as terminate_tree:
                            with self.assertRaisesRegex(RuntimeError, "认证失败"):
                                core.analyze_with_codex("value = 1\n", "source.py")
        self.assertEqual(process.communicate.call_count, 2)
        process.kill.assert_called_once()
        if os.name == "nt":
            terminate_tree.assert_called_once()
        self.assertLessEqual(process.communicate.call_args_list[0].kwargs["timeout"], 1)

    def test_stderr_tail_discards_truncated_source_json(self):
        path = Path(self.temporary.name) / "stderr.txt"
        source = "x" * 70000 + " ERROR: 401 Unauthorized invalid_api_key "
        echo = json.dumps({"filename": "source.py", "source": source})
        path.write_text(echo + "\nERROR: connection timed out\n", encoding="utf-8")
        tail = core._codex_error_tail(path)
        self.assertNotIn("invalid_api_key", tail)
        self.assertIn("connection timed out", tail)
        self.assertRegex(core.summarize_codex_error(tail, 1), "网络|连接|超时")


if __name__ == "__main__":
    unittest.main()
