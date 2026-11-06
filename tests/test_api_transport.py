from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_transport


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"has_errors": {"type": "boolean"}, "corrected_code": {"type": "string"}},
    "required": ["has_errors", "corrected_code"],
}
RESULT = {"has_errors": True, "corrected_code": "value = 2\n"}


def responses_result(result=RESULT):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "status": "completed", "content": [
            {"type": "output_text", "text": json.dumps(result)},
        ]},
    ]}


def chat_result(result=RESULT):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}


class ApiTransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.replies = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                test.calls.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
                reply = test.replies.pop(0) if test.replies else (500, {"error": "missing test response"}, {})
                if callable(reply):
                    reply = reply()
                status, value, headers = reply
                data = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = {
            "mode": "custom_api", "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
            "model": "provider-model", "api_format": "responses", "timeout": 3,
        }
        self.key = "test-only-secret-key"

    def analyze(self, config=None):
        return api_transport.analyze_api("Review the supplied source and return a JSON object.", SCHEMA, config or self.config, self.key)

    def test_responses_posts_schema_and_extracts_text(self):
        self.replies.append((200, responses_result(), {}))
        self.assertEqual(self.analyze(), RESULT)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["path"], "/v1/responses")
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + self.key)
        self.assertEqual(call["body"]["model"], "provider-model")
        self.assertFalse(call["body"]["store"])
        self.assertEqual(call["body"]["text"]["format"]["schema"], SCHEMA)
        self.assertTrue(call["body"]["text"]["format"]["strict"])

    def test_chat_completions_posts_messages_and_extracts_result(self):
        self.config["api_format"] = "chat_completions"
        self.replies.append((200, chat_result(), {}))
        self.assertEqual(self.analyze(), RESULT)
        call = self.calls[0]
        self.assertEqual(call["path"], "/v1/chat/completions")
        self.assertEqual(call["body"]["messages"][-1]["role"], "user")
        self.assertEqual(call["body"]["response_format"]["json_schema"]["schema"], SCHEMA)

    def test_endpoint_suffix_is_not_duplicated_and_protocol_can_change(self):
        for suffix in ("/responses", "/responses/", "/chat/completions", "/chat/completions/"):
            with self.subTest(suffix=suffix):
                config = {**self.config, "base_url": self.config["base_url"] + suffix}
                self.assertTrue(api_transport.build_api_url(config).endswith("/v1/responses"))
                config["api_format"] = "chat_completions"
                self.assertTrue(api_transport.build_api_url(config).endswith("/v1/chat/completions"))

    def test_official_api_ignores_custom_address(self):
        config = {**self.config, "mode": "openai_api", "base_url": "http://untrusted.invalid/v1"}
        self.assertEqual(api_transport.build_api_url(config), "https://api.openai.com/v1/responses")

    def test_invalid_addresses_are_rejected_before_request(self):
        for base in ("file:///private", "https://user:password@example.test/v1", "https://example.test/v1?secret=x", "https://example.test/v1#x", "https://example.test:99999/v1", "https:///v1", "http://a b/v1", "https://example.test\n/v1"):
            with self.subTest(base=base):
                with self.assertRaises(ValueError):
                    self.analyze({**self.config, "base_url": base})
        self.assertEqual(self.calls, [])

    def test_unknown_protocol_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "协议"):
            self.analyze({**self.config, "api_format": "unsupported"})
        self.assertEqual(self.calls, [])

    def test_explicit_schema_incompatibility_retries_same_endpoint_once(self):
        self.config["api_format"] = "chat_completions"
        self.replies.extend([
            (400, {"error": {"message": "response_format json_schema is unsupported"}}, {}),
            (200, chat_result(), {}),
        ])
        self.assertEqual(self.analyze(), RESULT)
        self.assertEqual([call["path"] for call in self.calls], ["/v1/chat/completions"] * 2)
        self.assertIn("response_format", self.calls[0]["body"])
        self.assertNotIn("response_format", self.calls[1]["body"])

    def test_responses_schema_incompatibility_can_retry_without_text_format(self):
        self.replies.extend([
            (422, {"error": {"message": "text.format json_schema is unsupported"}}, {}),
            (200, responses_result(), {}),
        ])
        self.assertEqual(self.analyze(), RESULT)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("text", self.calls[1]["body"])

    def test_schema_fallback_never_retries_more_than_once(self):
        self.replies.extend([(400, {"error": "json_schema unsupported"}, {})] * 3)
        with self.assertRaisesRegex(RuntimeError, "协议"):
            self.analyze()
        self.assertEqual(len(self.calls), 2)

    def test_unrelated_bad_request_does_not_retry(self):
        self.replies.append((400, {"error": {"message": "Invalid model parameter"}}, {}))
        with self.assertRaises(RuntimeError):
            self.analyze()
        self.assertEqual(len(self.calls), 1)

    def test_auth_error_cannot_be_mistaken_for_schema_incompatibility(self):
        self.replies.append((401, {"error": {"message": "Invalid secret response_format json_schema " + self.key}}, {}))
        with self.assertRaisesRegex(RuntimeError, "认证") as caught:
            self.analyze()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(self.key, str(caught.exception))
        self.assertNotIn("127.0.0.1", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_schema_mention_without_unsupported_error_does_not_retry(self):
        self.replies.append((400, {"error": {"message": "response_format json_schema supplied; invalid input length"}}, {}))
        with self.assertRaises(RuntimeError):
            self.analyze()
        self.assertEqual(len(self.calls), 1)

    def test_auth_code_on_bad_request_still_does_not_retry(self):
        self.replies.append((400, {"error": {"code": "invalid_api_key", "message": "unsupported json_schema " + self.key}}, {}))
        with self.assertRaisesRegex(RuntimeError, "认证"):
            self.analyze()
        self.assertEqual(len(self.calls), 1)

    def test_status_and_model_errors_have_specific_safe_messages(self):
        examples = [(429, "rate_limit_exceeded", "额度"), (404, "model_not_found", "模型"), (503, self.key, "暂时不可用")]
        for status, detail, expected in examples:
            with self.subTest(status=status):
                self.replies.append((status, {"error": detail}, {}))
                with self.assertRaisesRegex(RuntimeError, expected) as caught:
                    self.analyze()
                self.assertNotIn(self.key, str(caught.exception))

    def test_redirect_is_rejected_without_sending_another_request(self):
        self.replies.append((307, {}, {"Location": self.config["base_url"] + "/different-endpoint"}))
        with self.assertRaisesRegex(RuntimeError, "重定向"):
            self.analyze()
        self.assertEqual(len(self.calls), 1)

    def test_incomplete_response_is_rejected(self):
        value = responses_result()
        value["status"] = "incomplete"
        self.replies.append((200, value, {}))
        with self.assertRaisesRegex(ValueError, "完整"):
            self.analyze()

    def test_responses_tool_call_is_rejected(self):
        value = responses_result()
        value["output"].insert(0, {"type": "function_call", "name": "ignored"})
        self.replies.append((200, value, {}))
        with self.assertRaisesRegex(ValueError, "工具调用"):
            self.analyze()

    def test_refusal_is_rejected_without_disclosing_its_text(self):
        value = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": self.key}]}]}
        self.replies.append((200, value, {}))
        with self.assertRaisesRegex(ValueError, "拒绝") as caught:
            self.analyze()
        self.assertNotIn(self.key, str(caught.exception))

    def test_chat_truncation_and_tool_calls_are_rejected(self):
        self.config["api_format"] = "chat_completions"
        for update in ({"finish_reason": "length"}, {"message": {"content": "{}", "tool_calls": [{"id": "x"}]}}):
            with self.subTest(update=update):
                value = chat_result()
                value["choices"][0].update(update)
                self.replies.append((200, value, {}))
                with self.assertRaises(ValueError):
                    self.analyze()

    def test_exact_json_markdown_fence_is_accepted(self):
        self.config["api_format"] = "chat_completions"
        value = chat_result()
        value["choices"][0]["message"]["content"] = "```json\n" + json.dumps(RESULT) + "\n```"
        self.replies.append((200, value, {}))
        self.assertEqual(self.analyze(), RESULT)

    def test_non_json_text_is_not_copied_into_errors(self):
        self.config["api_format"] = "chat_completions"
        value = chat_result()
        value["choices"][0]["message"]["content"] = self.key
        self.replies.append((200, value, {}))
        with self.assertRaisesRegex(ValueError, "JSON") as caught:
            self.analyze()
        self.assertNotIn(self.key, str(caught.exception))

    def test_large_response_is_rejected(self):
        self.replies.append((200, b"x" * 300, {}))
        with patch.object(api_transport, "MAX_RESPONSE_BYTES", 256):
            with self.assertRaisesRegex(ValueError, "过大"):
                self.analyze()

    def test_total_timeout_bounds_response_and_returns_promptly(self):
        def delayed_reply():
            time.sleep(0.4)
            return 200, responses_result(), {}

        self.replies.append(delayed_reply)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "超时"):
            self.analyze({**self.config, "timeout": 0.08})
        self.assertLess(time.monotonic() - started, 0.3)

    def test_connection_failure_does_not_reveal_url_or_secret(self):
        unused = socket.socket()
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        unused.close()
        with self.assertRaisesRegex(RuntimeError, "网络") as caught:
            self.analyze({**self.config, "base_url": f"http://127.0.0.1:{port}/private-path"})
        self.assertNotIn("private-path", str(caught.exception))
        self.assertNotIn(self.key, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
