import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "源代码"
        self.folder.mkdir()
        self.state_dir = self.root / "state"
        self.output_dir = self.root / "桌面"
        self.output_dir.mkdir()
        self.default_opener = patch.object(core, "open_output", create=True, return_value=None)
        self.default_opener.start()
        self.addCleanup(self.default_opener.stop)

    def configure(self):
        return core.configure_folder(self.folder, self.state_dir)

    def scan(self, analyzer, opener=None):
        options = {"analyzer": analyzer, "output_dir": self.output_dir}
        if opener is not None:
            options["opener"] = opener
        return core.run_scan(self.state_dir, **options)

    def write(self, name, text):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def test_first_selection_baselines_existing_files_recursively(self):
        self.write("existing.py", "value = 1\n")
        self.write("package/old.py", "value = 2\n")
        self.configure()
        calls = []
        result = self.scan(lambda *args: calls.append(args))
        self.assertEqual(result["detected"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(Path(core.read_state(self.state_dir)["folder"]), self.folder)

    def test_new_and_changed_paths_are_checked_and_success_is_remembered(self):
        self.write("existing.py", "value = 1\n")
        self.configure()
        self.write("existing.py", "value = 99\n")
        self.write("package/new.py", "value = 3\n")
        calls = []

        def analyzer(source, filename):
            calls.append((source, filename))
            return {"has_errors": False, "corrected_code": ""}

        first = self.scan(analyzer)
        second = self.scan(analyzer)
        self.assertEqual(first["detected"], 2)
        self.assertEqual(first["checked"], 2)
        self.assertEqual(first["fixed"], 0)
        self.assertEqual(first["failed"], 0)
        self.assertEqual(second["detected"], 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual({source for source, _ in calls}, {"value = 3\n", "value = 99\n"})
        self.assertEqual({Path(filename).name for _, filename in calls}, {"new.py", "existing.py"})
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_repair_preserves_original_and_never_overwrites_desktop_file(self):
        self.configure()
        source = self.write("exercise.py", "result = 1 / 0\n")
        existing = self.output_dir / "exercise.py"
        existing.write_text("precious = True\n", encoding="utf-8")
        result = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": "result = 0\n"}
        )
        self.assertEqual(result["fixed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(source.read_text(encoding="utf-8"), "result = 1 / 0\n")
        self.assertEqual(existing.read_text(encoding="utf-8"), "precious = True\n")
        exported = Path(core.read_state(self.state_dir)["exports"][-1])
        self.assertEqual(exported.name, source.name)
        self.assertNotEqual(exported, existing)
        self.assertTrue(exported.is_relative_to(self.output_dir))
        self.assertEqual(exported.read_text(encoding="utf-8"), "result = 0\n")

    def test_analyzer_failure_is_retried_next_launch(self):
        self.configure()
        self.write("new.py", "answer = 42\n")

        def failure(*_):
            raise RuntimeError("offline")

        first = self.scan(failure)
        second = self.scan(
            lambda *_: {"has_errors": False, "corrected_code": ""}
        )
        self.assertEqual(first["failed"], 1)
        self.assertTrue(first["errors"])
        self.assertEqual(second["detected"], 1)
        self.assertEqual(second["checked"], 1)
        self.assertEqual(second["failed"], 0)
        self.assertEqual(self.scan(failure)["detected"], 0)

    def test_invalid_python_from_model_is_not_saved_or_acknowledged(self):
        self.configure()
        self.write("broken.py", "def f(:\n    pass\n")
        result = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": "def f(:\n    pass\n"}
        )
        self.assertEqual(result["fixed"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(list(self.output_dir.iterdir()), [])
        retry = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": "def f():\n    pass\n"}
        )
        self.assertEqual(retry["fixed"], 1)

    def test_definite_source_syntax_error_cannot_be_marked_clean(self):
        self.configure()
        self.write("broken.py", "def f(:\n    pass\n")
        result = self.scan(
            lambda *_: {"has_errors": False, "corrected_code": ""}
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(list(self.output_dir.iterdir()), [])
        retry = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": "def f():\n    pass\n"}
        )
        self.assertEqual(retry["fixed"], 1)

    def test_compile_invalid_model_output_is_rejected_even_when_ast_parses(self):
        self.configure()
        self.write("broken.py", "value = 1 / 0\n")
        result = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": "return 0\n"}
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["fixed"], 0)
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_new_comments_and_explanation_docstrings_are_rejected(self):
        self.configure()
        self.write("broken.py", "answer = 1 / 0\n")
        for corrected in (
            "# Fixed the division by zero\nanswer = 0\n",
            '"""The zero division was fixed."""\nanswer = 0\n',
        ):
            with self.subTest(corrected=corrected):
                result = self.scan(
                    lambda *_: {"has_errors": True, "corrected_code": corrected}
                )
                self.assertEqual(result["failed"], 1)
                self.assertEqual(result["fixed"], 0)
                self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_existing_comments_and_docstrings_may_be_retained(self):
        self.configure()
        self.write("broken.py", '"""Existing documentation."""\n# existing\nanswer = 1 / 0\n')
        corrected = '"""Existing documentation."""\n# existing\nanswer = 0\n'
        result = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": corrected}
        )
        self.assertEqual(result["fixed"], 1)
        self.assertEqual(result["failed"], 0)

    def test_source_change_during_analysis_defers_the_result(self):
        self.configure()
        source = self.write("new.py", "value = 1 / 0\n")

        def racing_analyzer(*_):
            source.write_text("value = 99\n", encoding="utf-8")
            return {"has_errors": True, "corrected_code": "value = 0\n"}

        first = self.scan(racing_analyzer)
        self.assertEqual(first["fixed"], 0)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(list(self.output_dir.iterdir()), [])
        second = self.scan(
            lambda *_: {"has_errors": False, "corrected_code": ""}
        )
        self.assertEqual(second["detected"], 1)
        self.assertEqual(second["checked"], 1)

    def test_dependency_and_metadata_folders_are_ignored(self):
        self.configure()
        for folder in (".git", "__pycache__", ".venv", "venv", "node_modules"):
            self.write(f"{folder}/ignored.py", "value = 1\n")
        self.write("package/keep.py", "value = 2\n")
        calls = []

        def analyzer(*args):
            calls.append(args)
            return {"has_errors": False, "corrected_code": ""}

        result = self.scan(analyzer)
        self.assertEqual(result["detected"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("keep.py", str(calls[0][1]))

    def test_symlinked_files_and_directories_are_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "external.py").write_text("external = True\n", encoding="utf-8")
        self.configure()
        try:
            (self.folder / "linked.py").symlink_to(outside / "external.py")
            (self.folder / "linked_dir").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(str(error))
        result = self.scan(lambda *_: self.fail("Symlinks must not be analyzed"))
        self.assertEqual(result["detected"], 0)

    def test_legacy_python_encoding_is_read_and_output_cookie_is_utf8(self):
        self.configure()
        source = self.folder / "编码.py"
        original = "# coding: gbk\n名称 = '中文'\nvalue = 1 / 0\n"
        source.write_bytes(original.encode("gbk"))
        calls = []

        def analyzer(text, filename):
            calls.append(text)
            return {
                "has_errors": True,
                "corrected_code": "# coding: gbk\n名称 = '中文'\nvalue = 0\n",
            }

        result = self.scan(analyzer)
        self.assertEqual(result["fixed"], 1)
        self.assertEqual(calls, [original])
        output = self.output_dir / "编码.py"
        data = output.read_bytes()
        self.assertIn("中文", data.decode("utf-8"))
        compile(data, str(output), "exec")
        self.assertEqual(source.read_bytes(), original.encode("gbk"))

    def test_same_stems_in_different_subfolders_get_distinct_outputs(self):
        self.configure()
        self.write("a/exercise.py", "answer = 1 / 0\n")
        self.write("b/exercise.py", "answer = 1 / 0\n")
        result = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": "answer = 0\n"}
        )
        self.assertEqual(result["fixed"], 2)
        exported = [Path(path) for path in core.read_state(self.state_dir)["exports"]]
        self.assertEqual(len(exported), 2)
        self.assertEqual(len(set(exported)), 2)
        self.assertEqual({path.name for path in exported}, {"exercise.py"})
        self.assertTrue(all(path.is_relative_to(self.output_dir) for path in exported))
        self.assertTrue(all(path.read_text(encoding="utf-8") == "answer = 0\n" for path in exported))

    def test_uppercase_python_suffix_and_filename_are_preserved_exactly(self):
        self.configure()
        self.write("Original.Name.PY", "value = 1 / 0\n")
        opened = []
        result = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": "value = 0\n"},
            opener=opened.append,
        )
        self.assertEqual(result["fixed"], 1)
        self.assertEqual(opened, [self.output_dir / "Original.Name.PY"])
        self.assertEqual(opened[0].name, "Original.Name.PY")

    def test_changed_same_filename_is_repaired_and_opened_again(self):
        self.configure()
        source = self.write("same_name.py", "value = 1 / 0\n")
        analyzed = []
        opened = []

        def analyzer(text, filename):
            analyzed.append((text, filename))
            return {"has_errors": True, "corrected_code": "value = 0\n"}

        first = self.scan(analyzer, opener=opened.append)
        self.assertEqual(first["fixed"], 1)
        source.write_text("value = 2 / 0\n", encoding="utf-8", newline="")
        second = self.scan(analyzer, opener=opened.append)
        self.assertEqual(second["detected"], 1)
        self.assertEqual(second["checked"], 1)
        self.assertEqual(second["fixed"], 1)
        self.assertEqual(len(analyzed), 2)
        self.assertEqual(analyzed[1][0], "value = 2 / 0\n")
        self.assertEqual(len(opened), 2)
        self.assertEqual({path.name for path in opened}, {"same_name.py"})
        self.assertNotEqual(opened[0], opened[1])
        self.assertTrue(all(path.read_text(encoding="utf-8") == "value = 0\n" for path in opened))
        self.assertEqual(source.read_text(encoding="utf-8"), "value = 2 / 0\n")
        self.assertEqual(self.scan(analyzer, opener=opened.append)["detected"], 0)
        self.assertEqual(len(analyzed), 2)
        self.assertEqual(len(opened), 2)

    def test_exported_outputs_inside_watch_folder_do_not_create_feedback_loop(self):
        self.configure()
        source = self.write("same_name.py", "value = 1 / 0\n")
        calls = []
        opened = []

        def analyzer(*args):
            calls.append(args)
            return {"has_errors": True, "corrected_code": "value = 0\n"}

        first = core.run_scan(
            self.state_dir, analyzer=analyzer, output_dir=self.folder, opener=opened.append
        )
        self.assertEqual(first["fixed"], 1)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].name, source.name)
        self.assertNotEqual(opened[0], source)
        self.assertTrue(opened[0].is_relative_to(self.folder))
        self.assertEqual(source.read_text(encoding="utf-8"), "value = 1 / 0\n")
        second = core.run_scan(
            self.state_dir, analyzer=analyzer, output_dir=self.folder, opener=opened.append
        )
        self.assertEqual(second["detected"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(opened), 1)

    def test_legacy_baseline_without_hash_is_snapshotted_before_tracking_edits(self):
        source = self.write("existing.py", "value = 1\n")
        self.configure()
        state = core.read_state(self.state_dir)
        for record in state["known"].values():
            record.pop("sha256", None)
        (self.state_dir / "state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        source.write_text("value = 2\n", encoding="utf-8", newline="")
        calls = []

        def analyzer(*args):
            calls.append(args)
            return {"has_errors": False, "corrected_code": ""}

        first = self.scan(analyzer)
        self.assertEqual(first["detected"], 0)
        self.assertEqual(calls, [])
        source.write_text("value = 3\n", encoding="utf-8", newline="")
        second = self.scan(analyzer)
        self.assertEqual(second["detected"], 1)
        self.assertEqual(second["checked"], 1)
        self.assertEqual(calls[0][0], "value = 3\n")

    def test_overlapping_instances_are_rejected_and_lock_is_reusable(self):
        with core.RunLock(self.state_dir):
            with self.assertRaises(core.AlreadyRunning):
                with core.RunLock(self.state_dir):
                    self.fail("A second instance acquired the lock")
        with core.RunLock(self.state_dir):
            pass

    def test_leading_chinese_comment_goals_are_extracted(self):
        source = (
            "#!/usr/bin/env python3\n"
            "# coding: utf-8\n"
            "\n"
            "# 功能要求：\n"
            "# 输入整数 n，输出 1 到 n 的和。\n"
            "# 不使用递归。\n"
            "\n"
            "total = 0\n"
            "# 内部说明不属于开头目标。\n"
        )
        goals = core.extract_goals(source)
        self.assertIn("功能要求", goals)
        self.assertIn("输入整数 n，输出 1 到 n 的和。", goals)
        self.assertIn("不使用递归。", goals)
        self.assertNotIn("python3", goals)
        self.assertNotIn("coding", goals)
        self.assertNotIn("内部说明", goals)

    def test_module_docstring_goals_are_extracted_without_function_docs(self):
        source = (
            '#!/usr/bin/python\n'
            '# -*- coding: utf-8 -*-\n'
            '# 课程目标\n'
            '"""功能：读取整数列表。\n'
            '要求：输出最大值。"""\n'
            '\n'
            'def maximum(values):\n'
            '    """函数内部说明。"""\n'
            '    return min(values)\n'
        )
        goals = core.extract_goals(source)
        self.assertIn("课程目标", goals)
        self.assertIn("功能：读取整数列表。", goals)
        self.assertIn("要求：输出最大值。", goals)
        self.assertNotIn("函数内部说明", goals)
        self.assertNotIn("coding", goals)

    def test_goal_extraction_survives_invalid_downstream_syntax(self):
        source = '# 目标：输出一到十。\n\n"""必须包含十。"""\n\ndef broken(:\n    pass\n'
        goals = core.extract_goals(source)
        self.assertIn("目标：输出一到十。", goals)
        self.assertIn("必须包含十。", goals)

    def test_interior_comments_and_function_docstrings_are_not_opening_goals(self):
        source = (
            'value = 1\n'
            '# 这个内部注释不作为目标。\n'
            'def helper():\n'
            '    """内部函数说明。"""\n'
            '    return value\n'
        )
        self.assertEqual(core.extract_goals(source).strip(), "")

    def test_saved_goal_repair_is_opened_after_complete_file_is_written(self):
        self.configure()
        original = "# 目标：输出 1 到 n 的整数之和，包含 n。\ndef total(n):\n    return sum(range(n))\n"
        corrected = "# 目标：输出 1 到 n 的整数之和，包含 n。\ndef total(n):\n    return sum(range(1, n + 1))\n"
        self.write("goals.py", original)
        opened = []

        def opener(path):
            self.assertIsInstance(path, Path)
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), corrected)
            compile(path.read_bytes(), str(path), "exec")
            opened.append(path)

        result = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": corrected},
            opener=opener,
        )
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["fixed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(opened, [self.output_dir / "goals.py"])
        self.assertEqual(
            core.extract_goals(opened[0].read_text(encoding="utf-8")),
            core.extract_goals(original),
        )

    def test_clean_and_failed_analysis_do_not_open_files(self):
        self.configure()
        self.write("clean.py", "value = 1\n")
        self.write("broken.py", "value = 1 / 0\n")
        opened = []

        def analyzer(source, filename):
            if str(filename).endswith("broken.py"):
                raise RuntimeError("offline")
            return {"has_errors": False, "corrected_code": ""}

        result = self.scan(analyzer, opener=opened.append)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["fixed"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(opened, [])

    def test_model_cannot_remove_opening_goals_and_file_is_retried(self):
        self.configure()
        header = "# 目标：输出从 1 到 n 的和，包含 n。\n"
        self.write("goals.py", header + "def total(n):\n    return sum(range(n))\n")
        opened = []
        corrected_body = "def total(n):\n    return sum(range(1, n + 1))\n"
        first = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": corrected_body},
            opener=opened.append,
        )
        self.assertEqual(first["failed"], 1)
        self.assertEqual(first["fixed"], 0)
        self.assertEqual(opened, [])
        self.assertEqual(list(self.output_dir.iterdir()), [])
        retry = self.scan(
            lambda *_: {"has_errors": True, "corrected_code": header + corrected_body},
            opener=opened.append,
        )
        self.assertEqual(retry["detected"], 1)
        self.assertEqual(retry["fixed"], 1)
        self.assertEqual(opened, [self.output_dir / "goals.py"])

    def test_failed_editor_open_retries_existing_file_without_reanalysis(self):
        self.configure()
        self.write("broken.py", "value = 1 / 0\n")
        analyzed = []

        def analyzer(*args):
            analyzed.append(args)
            return {"has_errors": True, "corrected_code": "value = 0\n"}

        def failing_opener(path):
            self.assertTrue(path.is_file())
            raise OSError("editor unavailable")

        first = self.scan(analyzer, opener=failing_opener)
        self.assertEqual(first["checked"], 1)
        self.assertEqual(first["fixed"], 1)
        self.assertTrue(first["errors"])
        self.assertTrue(any("editor unavailable" in str(error) for error in first["errors"]))
        output = self.output_dir / "broken.py"
        self.assertEqual(output.read_text(encoding="utf-8"), "value = 0\n")
        opened = []
        second = self.scan(analyzer, opener=opened.append)
        self.assertEqual(second["detected"], 0)
        self.assertEqual(second["fixed"], 0)
        self.assertEqual(len(analyzed), 1)
        self.assertEqual(opened, [output])
        self.assertEqual(list(self.output_dir.iterdir()), [output])
        third = self.scan(analyzer, opener=opened.append)
        self.assertEqual(third["detected"], 0)
        self.assertEqual(opened, [output])


class CodexErrorTests(unittest.TestCase):
    def test_uuid_and_timestamp_digits_do_not_imply_auth_or_rate_limit(self):
        detail = (
            "session id: 01a1401f-4290-74ac-8401-1256789abcde\n"
            "2026-11-04T20:14:01.401Z ERROR: request failed\n"
        )
        summary = core.summarize_codex_error(detail, 7)
        self.assertIn("调用失败", summary)
        self.assertIn("7", summary)
        self.assertNotIn("完成 Codex CLI 登录", summary)
        self.assertNotIn("使用额度或速率受到限制", summary)

    def test_explicit_401_unauthorized_is_reported_as_authentication(self):
        details = (
            "ERROR: unexpected HTTP status 401 Unauthorized: invalid authentication token",
            json.dumps({"type": "error", "error": {"status": 401, "code": "invalid_api_key", "message": "Authentication token expired"}}),
        )
        for detail in details:
            with self.subTest(detail=detail):
                summary = core.summarize_codex_error(detail, 1)
                self.assertRegex(summary, r"登录.*(失效|过期)|认证失败|需要.*登录|请先.*登录|重新.*登录")

    def test_connection_timeout_with_incidental_401_digits_is_network_failure(self):
        detail = (
            "2026-11-04T20:14:01.401Z session 01a1401f-1111-2222-3333-123456789abc\n"
            "ERROR: connection timed out while sending request to https://api.example.test/private\n"
        )
        summary = core.summarize_codex_error(detail, 1)
        self.assertRegex(summary, "网络|连接|超时")
        self.assertNotIn("完成 Codex CLI 登录", summary)

    def test_structured_model_error_is_classified_as_model_failure(self):
        detail = json.dumps({
            "type": "error",
            "error": {"code": "model_not_found", "message": "The requested model is not supported for this account"},
        })
        summary = core.summarize_codex_error(detail, 1)
        self.assertIn("模型", summary)
        self.assertNotIn("完成 Codex CLI 登录", summary)

    def test_structured_rate_limit_does_not_become_auth_from_timestamp(self):
        detail = (
            "2026-11-04T20:14:01.401Z\n"
            + json.dumps({"type": "error", "error": {"status_code": 429, "code": "rate_limit_exceeded", "message": "Too many requests"}})
        )
        summary = core.summarize_codex_error(detail, 1)
        self.assertRegex(summary, "额度|速率|限流|频率")
        self.assertNotIn("请检查网络、登录状态和使用额度", summary)
        self.assertNotIn("完成 Codex CLI 登录", summary)

    def test_echoed_source_keywords_do_not_override_real_network_error(self):
        source = "# model_not_found 401 Unauthorized 429 rate_limit_exceeded\npassword = 'sk-private-token-test'\n"
        detail = (
            "user\n"
            + json.dumps({"filename": "private_source.py", "opening_goals": "输出结果", "source": source})
            + "\nERROR: stream disconnected before completion: error sending request for url (https://secret.example.test/private)\n"
        )
        summary = core.summarize_codex_error(detail, 1)
        self.assertRegex(summary, "网络|连接|超时")
        self.assertNotIn("所选模型", summary)
        self.assertNotIn("完成 Codex CLI 登录", summary)
        for secret in ("private_source.py", "sk-private-token-test", "password", "secret.example.test"):
            self.assertNotIn(secret, summary)
        self.assertLess(len(summary), 240)

    def test_unsupported_transport_feature_is_not_reported_as_unsupported_model(self):
        summary = core.summarize_codex_error("ERROR: network socket feature not supported", 1)
        self.assertNotIn("所选模型", summary)

    def test_analysis_timeout_terminates_process_and_returns_retryable_error(self):
        process = Mock()
        process.pid = 987654
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 0.01), (None, None)]
        with patch.object(core, "find_codex", return_value=Path("codex.exe")):
            with patch.object(core, "_configured_model", return_value=None):
                with patch.object(core, "CODEX_TIMEOUT", 0.01):
                    with patch.object(core.subprocess, "Popen", return_value=process):
                        with patch.object(core.subprocess, "run") as terminate_tree:
                            with self.assertRaisesRegex(TimeoutError, "超时"):
                                core.analyze_with_codex("value = 1\n", "source.py")
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)
        if os.name == "nt":
            terminate_tree.assert_called_once()
            self.assertEqual(terminate_tree.call_args.args[0][:3], ["taskkill", "/PID", str(process.pid)])


@unittest.skipUnless(os.name == "nt", "Windows editor integration")
class EditorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "修复结果.py"
        self.source.write_text("value = 1\n", encoding="utf-8")

    def test_windows_launches_pycharm_directly_with_visible_window(self):
        editor = Path(self.temporary.name) / "PyCharm Professional 2026.3" / "bin" / "pycharm64.exe"
        with patch.object(core, "find_pycharm", return_value=editor):
            with patch.object(core.os, "startfile") as startfile:
                with patch.object(core.subprocess, "Popen") as popen:
                    core.open_output(self.source)
        popen.assert_called_once()
        arguments = popen.call_args.args[0]
        self.assertEqual(arguments, [str(editor), "--line", "1", str(self.source.resolve())])
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        startfile.assert_not_called()
        startup = popen.call_args.kwargs["startupinfo"]
        self.assertTrue(startup.dwFlags & core.subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startup.wShowWindow, 1)

    def test_missing_pycharm_raises_without_launching_another_editor(self):
        with patch.object(core, "find_pycharm", side_effect=FileNotFoundError("PyCharm unavailable")):
            with patch.object(core.os, "startfile") as startfile:
                with patch.object(core.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(FileNotFoundError, "PyCharm unavailable"):
                        core.open_output(self.source)
        startfile.assert_not_called()
        popen.assert_not_called()

    def test_frozen_launch_restores_dll_directory_when_process_creation_fails(self):
        editor = Path(self.temporary.name) / "PyCharm" / "bin" / "pycharm64.exe"
        for previous in (r"C:\Frozen App\_internal", ""):
            with self.subTest(previous=previous):
                events = []

                def get_directory(capacity, buffer):
                    self.assertGreater(capacity, len(previous))
                    buffer.value = previous
                    return len(previous)

                def set_directory(value):
                    events.append(("dll", value))
                    return 1

                def failed_launch(*args, **kwargs):
                    self.assertEqual(events, [("dll", None)])
                    events.append(("launch", None))
                    raise OSError("process creation failed")

                with patch.object(core.sys, "frozen", True, create=True):
                    with patch.object(core.ctypes, "windll") as windll:
                        windll.kernel32.GetDllDirectoryW.side_effect = get_directory
                        windll.kernel32.SetDllDirectoryW.side_effect = set_directory
                        with patch.object(core, "find_pycharm", return_value=editor):
                            with patch.object(core.subprocess, "Popen", side_effect=failed_launch):
                                with self.assertRaisesRegex(OSError, "process creation failed"):
                                    core.open_output(self.source)
                        windll.kernel32.GetDllDirectoryW.assert_called_once()
                self.assertEqual(
                    events,
                    [("dll", None), ("launch", None), ("dll", previous or None)],
                )

    def discovery_environment(self, override):
        root = Path(self.temporary.name)
        return {
            "PY_AUTO_REPAIR_PYCHARM": str(override),
            "ProgramFiles": str(root / "Program Files"),
            "ProgramFiles(x86)": str(root / "Program Files (x86)"),
            "ProgramW6432": str(root / "Program Files"),
            "LOCALAPPDATA": str(root / "Local"),
            "APPDATA": str(root / "Roaming"),
            "USERPROFILE": str(root),
        }

    def test_configured_pycharm_native_executable_is_discovered(self):
        editor = Path(self.temporary.name) / "PyCharm Test With Spaces" / "bin" / "pycharm64.exe"
        editor.parent.mkdir(parents=True)
        editor.write_bytes(b"not executed")
        with patch.dict(core.os.environ, self.discovery_environment(editor), clear=True):
            with patch.object(core.shutil, "which", return_value=None):
                with patch.object(core.Path, "home", return_value=Path(self.temporary.name)):
                    self.assertEqual(core.find_pycharm(), editor.resolve())

    def test_batch_launcher_is_not_accepted_as_native_pycharm(self):
        batch = Path(self.temporary.name) / "pycharm.bat"
        batch.write_text("@exit /b 0\n", encoding="utf-8")
        with patch.dict(core.os.environ, self.discovery_environment(batch), clear=True):
            with patch.object(core.shutil, "which", return_value=None):
                with patch.object(core.Path, "home", return_value=Path(self.temporary.name)):
                    with self.assertRaises(FileNotFoundError):
                        core.find_pycharm()

    def test_missing_configured_pycharm_is_not_reported_as_installed(self):
        missing = Path(self.temporary.name) / "missing" / "pycharm64.exe"
        with patch.dict(core.os.environ, self.discovery_environment(missing), clear=True):
            with patch.object(core.shutil, "which", return_value=None):
                with patch.object(core.Path, "home", return_value=Path(self.temporary.name)):
                    with self.assertRaises(FileNotFoundError):
                        core.find_pycharm()


if __name__ == "__main__":
    unittest.main()
