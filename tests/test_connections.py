import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import connections


class RecordedStream(io.StringIO):
    def close(self):
        if not self.closed:
            self.recorded = self.getvalue()
        super().close()


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "settings"

    def configuration(self, **changes):
        config = {
            "mode": "custom_api",
            "base_url": "https://first.example.test/v1",
            "model": "model-for-tests",
            "api_format": "responses",
        }
        config.update(changes)
        return config

    def document(self):
        return json.loads((self.state_dir / "connection.json").read_text(encoding="utf-8"))

    def test_default_connection_is_chatgpt_without_exposing_credentials(self):
        config = connections.read_connection(self.state_dir)
        self.assertEqual(config["mode"], "chatgpt")
        self.assertFalse(config["has_api_key"])
        self.assertNotIn("api_key", config)
        self.assertNotIn("api_key_encrypted", config)
        self.assertFalse(self.state_dir.exists())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_dpapi_round_trip_and_file_contains_no_plaintext_secret(self):
        secret = "sk-local-only-connection-test-36e0fc53"
        config = connections.save_connection(self.state_dir, self.configuration(), api_key=secret)
        document = self.document()
        encrypted = base64.b64decode(document["api_key_encrypted"], validate=True)
        self.assertNotEqual(encrypted, secret.encode("utf-8"))
        self.assertEqual(connections._protect(encrypted, decrypt=True).decode("utf-8"), secret)
        self.assertEqual(connections.get_api_key(self.state_dir), secret)
        self.assertNotIn(secret, (self.state_dir / "connection.json").read_text(encoding="utf-8"))
        self.assertNotIn(secret, json.dumps(config))
        self.assertTrue(config["has_api_key"])
        self.assertNotIn("api_key_encrypted", config)
        self.assertEqual(list(self.state_dir.glob("connection-*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_blank_key_preserves_same_service_but_new_endpoint_requires_key(self):
        secret = "sk-same-service-key-test"
        connections.save_connection(self.state_dir, self.configuration(), api_key=secret)
        saved = connections.save_connection(self.state_dir, self.configuration(model="another-model"), api_key="")
        self.assertTrue(saved["has_api_key"])
        self.assertEqual(connections.get_api_key(self.state_dir), secret)
        previous = self.document()
        for new_url in ("https://second.example.test/v1", "https://first.example.test/another-endpoint"):
            with self.subTest(url=new_url):
                with self.assertRaisesRegex(ValueError, "密钥"):
                    connections.save_connection(self.state_dir, self.configuration(base_url=new_url), api_key="")
                self.assertEqual(self.document(), previous)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_switching_api_mode_does_not_reuse_previous_provider_key(self):
        connections.save_connection(self.state_dir, self.configuration(), api_key="sk-provider-isolation-test")
        with self.assertRaisesRegex(ValueError, "密钥"):
            connections.save_connection(self.state_dir, self.configuration(mode="openai_api"), api_key="")
        self.assertEqual(self.document()["mode"], "custom_api")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_chatgpt_mode_removes_api_key_and_ignores_supplied_key(self):
        connections.save_connection(self.state_dir, self.configuration(), api_key="sk-previous-provider-test")
        config = connections.save_connection(
            self.state_dir,
            self.configuration(mode="chatgpt", base_url="https://unused.example.test", api_format="chat_completions"),
            api_key="sk-ignored-for-chatgpt-test",
        )
        self.assertEqual(config["mode"], "chatgpt")
        self.assertEqual(config["base_url"], connections.OFFICIAL_URL)
        self.assertEqual(config["api_format"], "responses")
        self.assertFalse(config["has_api_key"])
        self.assertEqual(self.document()["api_key_encrypted"], "")
        with self.assertRaisesRegex(ValueError, "密钥"):
            connections.get_api_key(self.state_dir)

    def test_invalid_custom_urls_are_rejected_before_writing(self):
        urls = (
            "api.example.test/v1",
            "ftp://api.example.test/v1",
            "https://user:password@example.test/v1",
            "https://api.example.test/v1?api_key=secret",
            "https://api.example.test/v1#fragment",
            "https://api.example.test:invalid/v1",
            "https://api.example.test/path with spaces",
            "https:///v1",
        )
        for index, url in enumerate(urls):
            with self.subTest(url=url):
                candidate_dir = self.state_dir / str(index)
                with self.assertRaisesRegex(ValueError, "HTTPS|地址"):
                    connections.save_connection(candidate_dir, self.configuration(base_url=url), api_key="test-key")
                self.assertFalse((candidate_dir / "connection.json").exists())

    def test_custom_http_is_supported_and_official_mode_fixes_endpoint(self):
        for url in ("http://localhost:8080/v1", "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1", "http://gateway.example.test/v1"):
            with self.subTest(url=url):
                self.assertEqual(connections._normalize(self.configuration(base_url=url))["base_url"], url)
        config = connections._normalize(self.configuration(mode="openai_api", base_url="https://unused.example.test"))
        self.assertEqual(config["base_url"], connections.OFFICIAL_URL)

    def test_dedicated_login_environment_isolated_from_parent_settings(self):
        login_home = self.state_dir / "codex-home"
        login_home.mkdir(parents=True)
        (login_home / "auth.json").write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
        environment = {key: "private-test-value" for key in connections.SECRET_ENVIRONMENT}
        environment.update({"CODEX_HOME": str(Path(self.temporary.name) / "global-codex"), "UNRELATED": "retained"})
        with patch.dict(connections.os.environ, environment, clear=True):
            child = connections.codex_environment(self.state_dir)
            self.assertEqual(dict(connections.os.environ), environment)
        self.assertEqual(child["CODEX_HOME"], str(login_home))
        self.assertEqual(child["UNRELATED"], "retained")
        for key in connections.SECRET_ENVIRONMENT:
            self.assertNotIn(key, child)

    def test_chatgpt_readiness_requires_chatgpt_login_and_sanitizes_failure(self):
        result = Mock(returncode=0, stdout=b"Logged in using ChatGPT\n", stderr=b"")
        with patch.object(connections.subprocess, "run", return_value=result):
            connections.ensure_connection(self.state_dir, lambda: Path("codex.exe"))
        secret = "sk-error-text-must-not-appear"
        result = Mock(returncode=1, stdout=b"", stderr=("API key: " + secret).encode())
        with patch.object(connections.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "ChatGPT") as error:
                connections.ensure_connection(self.state_dir, lambda: Path("codex.exe"))
        self.assertNotIn(secret, str(error.exception))


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "settings"
        self.chrome = Path(self.temporary.name) / "Chrome" / "chrome.exe"
        self.cli = Path(self.temporary.name) / "codex.exe"

    def server(self, messages):
        server = Mock()
        server.stdin = RecordedStream()
        server.stdout = RecordedStream("".join(json.dumps(message) + "\n" for message in messages))
        server.poll.return_value = None
        server.wait.return_value = 0
        return server

    def test_appserver_opens_authorization_only_in_explicit_chrome(self):
        auth_url = "https://auth.openai.com/oauth/authorize?state=private-test-state"
        server = self.server([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"authUrl": auth_url, "loginId": "login-for-test"}},
            {"method": "account/login/completed", "params": {"success": True, "loginId": "login-for-test"}},
        ])
        statuses = []
        with patch.object(connections, "_chrome", return_value=self.chrome):
            with patch.object(connections, "_external_process", side_effect=[server, Mock()]) as launch:
                with patch.object(connections, "ensure_connection") as ensure:
                    result = connections.login_chatgpt(self.state_dir, lambda: self.cli, statuses.append)
        self.assertIn("登录成功", result)
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(launch.call_args_list[1].args[0], [str(self.chrome), "--new-tab", auth_url])
        self.assertNotIn("edge", repr(launch.call_args_list).lower())
        self.assertEqual(launch.call_args_list[0].kwargs["env"]["CODEX_HOME"], str(self.state_dir / "codex-home"))
        sent = [json.loads(line) for line in server.stdin.recorded.splitlines()]
        self.assertEqual([message["method"] for message in sent], ["initialize", "initialized", "account/login/start"])
        self.assertEqual(sent[-1]["params"], {"type": "chatgpt"})
        self.assertEqual(ensure.call_count, 1)
        server.terminate.assert_called_once()
        self.assertTrue(server.stdin.closed)
        self.assertTrue(server.stdout.closed)
        self.assertEqual(len(statuses), 1)
        self.assertNotIn("private-test-state", statuses[0])

    def test_untrusted_authorization_url_is_rejected_without_browser_or_secret_leak(self):
        secret = "sk-never-log-this-value"
        server = self.server([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"authUrl": "https://untrusted.example.test/authorize?key=" + secret}},
        ])
        with patch.object(connections, "_chrome", return_value=self.chrome):
            with patch.object(connections, "_external_process", return_value=server) as launch:
                with patch.object(connections, "ensure_connection") as ensure:
                    with self.assertRaisesRegex(RuntimeError, "授权链接") as error:
                        connections.login_chatgpt(self.state_dir, lambda: self.cli)
        self.assertEqual(launch.call_count, 1)
        ensure.assert_not_called()
        server.terminate.assert_called_once()
        self.assertNotIn(secret, str(error.exception))
        self.assertNotIn("untrusted.example.test", str(error.exception))

    def test_missing_chrome_does_not_fall_back_to_edge(self):
        with patch.object(connections, "_chrome", side_effect=FileNotFoundError("未找到 Chrome")):
            with patch.object(connections, "_external_process") as launch:
                with self.assertRaisesRegex(FileNotFoundError, "Chrome"):
                    connections.login_chatgpt(self.state_dir, lambda: self.cli)
        launch.assert_not_called()

    def test_failed_oauth_completion_is_reported_without_server_error_details(self):
        secret = "private-error-from-provider"
        server = self.server([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"authUrl": "https://auth.openai.com/oauth/authorize?state=test"}},
            {"method": "account/login/completed", "params": {"success": False, "error": secret}},
        ])
        with patch.object(connections, "_chrome", return_value=self.chrome):
            with patch.object(connections, "_external_process", side_effect=[server, Mock()]):
                with patch.object(connections, "ensure_connection") as ensure:
                    with self.assertRaisesRegex(RuntimeError, "授权未完成") as error:
                        connections.login_chatgpt(self.state_dir, lambda: self.cli)
        ensure.assert_not_called()
        self.assertNotIn(secret, str(error.exception))
        server.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
