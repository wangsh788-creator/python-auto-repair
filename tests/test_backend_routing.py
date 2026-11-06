from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_transport
import connections
import core


SOURCE = "# 功能目标：返回输入整数的平方，不改变接口。\ndef square(number):\n    return number\n"
CORRECTED = "# 功能目标：返回输入整数的平方，不改变接口。\ndef square(number):\n    return number ** 2\n"
RESULT = {"has_errors": True, "corrected_code": CORRECTED}


class BackendRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"

    def test_api_modes_route_without_codex_or_chatgpt_auth(self):
        for mode in ("openai_api", "custom_api"):
            with self.subTest(mode=mode):
                config = {"mode": mode, "model": "test-provider-model", "base_url": "https://example.invalid/v1", "api_format": "responses"}
                with patch.object(connections, "read_connection", return_value=config):
                    with patch.object(connections, "get_api_key", return_value="local-test-key") as get_key:
                        with patch.object(core, "find_codex", side_effect=AssertionError("API mode must not resolve Codex")) as find_cli:
                            with patch.object(connections, "codex_environment", side_effect=AssertionError("API mode must not inspect ChatGPT auth")) as chat_auth:
                                with patch.object(core, "analyze_with_codex", side_effect=AssertionError("API mode must not launch Codex")) as codex:
                                    with patch.object(api_transport, "analyze_api", return_value=RESULT) as analyze:
                                        actual = core.analyze_with_backend(SOURCE, "original.py", self.state_dir)
                self.assertEqual(actual, RESULT)
                find_cli.assert_not_called()
                chat_auth.assert_not_called()
                codex.assert_not_called()
                get_key.assert_called_once_with(self.state_dir)
                instruction, schema, received_config, received_key = analyze.call_args.args
                self.assertIn("original.py", instruction)
                self.assertIn(core.extract_goals(SOURCE), instruction)
                self.assertIn(json.dumps(SOURCE, ensure_ascii=False), instruction)
                self.assertEqual(schema, core.RESPONSE_SCHEMA)
                self.assertEqual(received_config, config)
                self.assertEqual(received_key, "local-test-key")

    def test_chatgpt_mode_uses_codex_and_skips_api_secrets(self):
        for configured_model in ("", "chosen-chatgpt-model"):
            with self.subTest(model=configured_model):
                config = {"mode": "chatgpt", "model": configured_model}
                with patch.object(connections, "read_connection", return_value=config):
                    with patch.object(connections, "ensure_connection", return_value=None):
                        with patch.object(connections, "get_api_key", side_effect=AssertionError("ChatGPT must not read API secrets")) as get_key:
                            with patch.object(api_transport, "analyze_api", side_effect=AssertionError("ChatGPT must not call direct API")) as api:
                                with patch.object(core, "analyze_with_codex", return_value=RESULT) as codex:
                                    actual = core.analyze_with_backend(SOURCE, "original.py", self.state_dir)
                self.assertEqual(actual, RESULT)
                codex.assert_called_once_with(SOURCE, "original.py", state_dir=self.state_dir, model=configured_model or None)
                get_key.assert_not_called()
                api.assert_not_called()

    def test_chatgpt_readiness_failure_prevents_analysis(self):
        with patch.object(connections, "read_connection", return_value={"mode": "chatgpt", "model": ""}):
            with patch.object(connections, "ensure_connection", side_effect=RuntimeError("请重新使用 ChatGPT 登录")):
                with patch.object(core, "analyze_with_codex") as analyze:
                    with self.assertRaisesRegex(RuntimeError, "ChatGPT 登录"):
                        core.analyze_with_backend(SOURCE, "original.py", self.state_dir)
        analyze.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
class ApiScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.folder = self.root / "source"
        self.folder.mkdir()
        self.output_dir = self.root / "desktop"
        self.output_dir.mkdir()
        self.calls = []
        self.protocol = "responses"
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                test.calls.append((self.path, dict(self.headers), payload))
                text = json.dumps(RESULT, ensure_ascii=False)
                if test.protocol == "responses":
                    value = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
                else:
                    value = {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}
                raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def exercise_protocol(self, protocol):
        self.protocol = protocol
        self.calls.clear()
        state_dir = self.state_dir / protocol
        folder = self.folder / protocol
        folder.mkdir()
        output_dir = self.output_dir / protocol
        output_dir.mkdir()
        core.configure_folder(folder, state_dir)
        source_path = folder / "平方测试.PY"
        source_path.write_text(SOURCE, encoding="utf-8", newline="")
        config = {
            "mode": "custom_api", "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
            "api_format": protocol, "model": "test-local-model",
        }
        secret = "local-integration-secret-not-a-real-key"
        connections.save_connection(state_dir, config, api_key=secret)
        self.assertEqual(connections.get_api_key(state_dir), secret)
        self.assertNotIn(secret, (state_dir / "connection.json").read_text(encoding="utf-8"))
        opened = []

        def opener(path):
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), CORRECTED)
            opened.append(path)

        with patch.object(core, "find_codex", side_effect=AssertionError("Codex is deliberately unavailable")) as cli:
            with patch.object(connections, "codex_environment", side_effect=AssertionError("No ChatGPT auth access")) as auth:
                result = core.run_scan(state_dir, output_dir=output_dir, opener=opener)
        self.assertEqual(result, {"detected": 1, "checked": 1, "fixed": 1, "failed": 0, "errors": []})
        self.assertEqual(opened, [output_dir / source_path.name])
        self.assertEqual(opened[0].name, source_path.name)
        self.assertEqual(source_path.read_text(encoding="utf-8"), SOURCE)
        self.assertEqual(core.extract_goals(opened[0].read_text(encoding="utf-8")), core.extract_goals(SOURCE))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["Authorization"], "Bearer " + secret)
        endpoint = "/responses" if protocol == "responses" else "/chat/completions"
        self.assertEqual(self.calls[0][0], "/v1" + endpoint)
        cli.assert_not_called()
        auth.assert_not_called()
        state = core.read_state(state_dir)
        self.assertEqual(state["known"][source_path.name]["status"], "fixed")
        self.assertEqual(state["pending_open"], [])

    def test_responses_api_saves_same_name_and_opens_completed_file(self):
        self.exercise_protocol("responses")

    def test_chat_completions_api_saves_same_name_and_opens_completed_file(self):
        self.exercise_protocol("chat_completions")


if __name__ == "__main__":
    unittest.main()
