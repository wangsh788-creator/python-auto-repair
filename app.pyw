import argparse
import datetime
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from connections import (
    check_connection,
    connection_summary,
    ensure_connection,
    login_chatgpt,
    read_connection,
    save_connection,
)
from core import (
    AlreadyRunning,
    RunLock,
    configure_folder,
    default_state_dir,
    desktop_dir,
    find_codex,
    read_state,
    run_scan,
)


MODE_LABELS = {
    "chatgpt": "ChatGPT 登录（Codex）",
    "openai_api": "官方 OpenAI API",
    "custom_api": "自定义 / 中转 API",
}
FORMAT_LABELS = {"responses": "Responses API", "chat_completions": "Chat Completions"}


def log_error(state_dir, message):
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / "error.log").open("a", encoding="utf-8") as stream:
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            stream.write(f"{stamp} {message}\n")
    except OSError:
        pass


def concise_error(message):
    text = " ".join(str(message).split())
    return text if len(text) <= 280 else text[:277] + "…"


def select_initial_folder(state_dir, missing=False):
    root = tk.Tk()
    root.withdraw()
    try:
        title = "原文件夹不存在，请选择新的文件夹" if missing else "选择要检查的 Python 文件夹"
        selected = filedialog.askdirectory(parent=root, title=title, mustexist=True)
        if not selected:
            return False
        configure_folder(Path(selected), state_dir)
        messagebox.showinfo(
            "Python 自动修复",
            "已记住文件夹及现有文件。\n以后启动会后台检查新增或内容变化的 .py 文件，修复结果保留文件名保存到桌面并自动用 VS Code 打开查看。",
            parent=root,
        )
        return True
    finally:
        root.destroy()


class SettingsWindow:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.root = tk.Tk()
        self.root.title("Python 自动修复设置")
        self.root.geometry("740x560")
        self.root.minsize(660, 540)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.events = queue.Queue()
        self.busy = False
        self.close_when_done = False
        self.corrupt_state = False
        self.started_at = None
        self.progress_event = {}
        self.activity = None
        self.redact_values = ()
        try:
            state = read_state(state_dir)
            initial_status = "就绪"
            previous = state.get("last_result", {})
            if isinstance(previous, dict) and previous.get("failed"):
                previous_errors = previous.get("errors") or ["上次检查未完成，请重新检查。"]
                initial_status = "上次检查失败：" + concise_error(previous_errors[0])
        except RuntimeError as exc:
            state = {}
            self.corrupt_state = True
            initial_status = "设置记录损坏，请重新选择文件夹；保存前将保留备份。"
            log_error(state_dir, str(exc))
        try:
            connection = read_connection(state_dir)
        except Exception as exc:
            connection = {"mode": "chatgpt", "model": "", "base_url": "", "api_format": "responses"}
            initial_status = "连接设置无法读取：" + concise_error(exc)
        self.saved_connection = connection
        self.folder = tk.StringVar(value=str(state.get("folder") or ""))
        self.status = tk.StringVar(value=initial_status)
        self.mode = tk.StringVar(value=MODE_LABELS.get(connection.get("mode"), MODE_LABELS["chatgpt"]))
        self.base_url = tk.StringVar(value=str(connection.get("base_url") or "https://api.openai.com/v1"))
        self.model = tk.StringVar(value=str(connection.get("model") or ""))
        self.api_format = tk.StringVar(value=FORMAT_LABELS.get(connection.get("api_format"), FORMAT_LABELS["responses"]))
        self.api_key = tk.StringVar(value="")
        self.connection_status = tk.StringVar(value="")
        self.connection_hint = tk.StringVar(value="")
        self.key_hint = tk.StringVar(value="")
        panel = ttk.Frame(self.root, padding=20)
        panel.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(panel)
        self.notebook.pack(fill="both", expand=True)
        scan_panel = ttk.Frame(self.notebook, padding=16)
        connection_panel = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(scan_panel, text="文件检查")
        self.notebook.add(connection_panel, text="模型连接")
        ttk.Label(scan_panel, text="检查文件夹").grid(row=0, column=0, sticky="w")
        ttk.Entry(scan_panel, textvariable=self.folder, state="readonly").grid(
            row=1, column=0, sticky="ew", pady=(6, 10)
        )
        self.choose_button = ttk.Button(scan_panel, text="选择文件夹", command=self.choose)
        self.choose_button.grid(row=1, column=1, padx=(10, 0), pady=(6, 10))
        ttk.Label(
            scan_panel,
            text="包含子文件夹；检查新增或内容变化的 .py 文件是否实现开头注释中的目标。",
            wraplength=610,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 9))
        ttk.Label(scan_panel, text=f"修复结果：{desktop_dir()}，保留原文件名并自动用 VS Code 打开。", wraplength=610).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(0, 13)
        )
        ttk.Label(scan_panel, textvariable=self.connection_status, wraplength=610).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 13)
        )
        self.scan_button = ttk.Button(scan_panel, text="检查新增及变化文件", command=self.scan)
        self.scan_button.grid(row=5, column=0, sticky="w")
        ttk.Label(
            scan_panel,
            text="首次选择文件夹会记录现有文件。之后每次启动检查一轮，新增文件或修改内容后可再次检查。",
            wraplength=610,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(16, 0))
        scan_panel.columnconfigure(0, weight=1)
        ttk.Label(connection_panel, text="连接方式").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.mode_box = ttk.Combobox(connection_panel, textvariable=self.mode, values=list(MODE_LABELS.values()), state="readonly")
        self.mode_box.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        self.mode_box.bind("<<ComboboxSelected>>", self.mode_changed)
        ttk.Label(connection_panel, text="模型名称").grid(row=1, column=0, sticky="w", pady=6)
        self.model_entry = ttk.Entry(connection_panel, textvariable=self.model)
        self.model_entry.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(connection_panel, text="API 地址").grid(row=2, column=0, sticky="w", pady=6)
        self.url_entry = ttk.Entry(connection_panel, textvariable=self.base_url)
        self.url_entry.grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Label(connection_panel, text="API 协议").grid(row=3, column=0, sticky="w", pady=6)
        self.format_box = ttk.Combobox(connection_panel, textvariable=self.api_format, values=list(FORMAT_LABELS.values()), state="readonly")
        self.format_box.grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Label(connection_panel, text="API 密钥").grid(row=4, column=0, sticky="w", pady=6)
        self.key_entry = ttk.Entry(connection_panel, textvariable=self.api_key, show="•")
        self.key_entry.grid(row=4, column=1, sticky="ew", pady=6)
        ttk.Label(connection_panel, textvariable=self.key_hint, wraplength=520).grid(row=5, column=1, sticky="w", pady=(0, 8))
        ttk.Label(connection_panel, textvariable=self.connection_hint, wraplength=610).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(4, 12)
        )
        connection_buttons = ttk.Frame(connection_panel)
        connection_buttons.grid(row=7, column=0, columnspan=2, sticky="w")
        self.save_button = ttk.Button(connection_buttons, text="保存连接设置", command=lambda: self.save_current_connection(announce=True))
        self.save_button.pack(side="left")
        self.test_button = ttk.Button(connection_buttons, text="测试连接", command=self.test_connection)
        self.test_button.pack(side="left", padx=(10, 0))
        self.login_button = ttk.Button(connection_panel, text="用 Chrome 登录 ChatGPT", command=self.start_login)
        self.login_button.grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))
        connection_panel.columnconfigure(1, weight=1)
        connection_panel.columnconfigure(0, minsize=90)
        self.progress_bar = ttk.Progressbar(panel, mode="indeterminate")
        self.progress_bar.pack(fill="x", pady=(13, 0))
        ttk.Label(panel, textvariable=self.status, wraplength=660).pack(fill="x", pady=(13, 0))
        self.mode_changed()
        self.refresh_connection_status()
        self.root.after(120, self.poll)

    def selected_mode(self):
        return next((mode for mode, label in MODE_LABELS.items() if label == self.mode.get()), "chatgpt")

    def selected_format(self):
        return next((name for name, label in FORMAT_LABELS.items() if label == self.api_format.get()), "responses")

    def mode_changed(self, _event=None):
        mode = self.selected_mode()
        is_api = mode != "chatgpt"
        if mode == "openai_api":
            self.base_url.set("https://api.openai.com/v1")
        self.url_entry.configure(state="normal" if mode == "custom_api" and not self.busy else "disabled")
        self.format_box.configure(state="readonly" if is_api and not self.busy else "disabled")
        self.key_entry.configure(state="normal" if is_api and not self.busy else "disabled")
        self.model_entry.configure(state="disabled" if self.busy else "normal")
        self.login_button.configure(state="normal" if mode == "chatgpt" and not self.busy else "disabled")
        same_provider = self.saved_connection.get("mode") == mode and (
            mode != "custom_api" or self.saved_connection.get("base_url", "").rstrip("/") == self.base_url.get().strip().rstrip("/")
        )
        self.key_hint.set("已有保存的密钥；留空会保留。" if same_provider and self.saved_connection.get("has_api_key") else "在此输入本机要使用的 API 密钥。")
        if mode == "chatgpt":
            self.connection_hint.set("通过 Codex 在后台分析。模型留空时自动选择；首次使用可点击下方按钮，在 Chrome 中完成 ChatGPT 授权。")
        elif mode == "openai_api":
            self.connection_hint.set("直接调用官方 API。填写账号可用的模型名称和 API 密钥，再测试连接。")
        else:
            self.connection_hint.set("填写服务商提供的 API 地址、模型和密钥，并选择其支持的协议。直接调用该服务进行分析。")

    def refresh_connection_status(self):
        try:
            self.connection_status.set("当前连接：" + connection_summary(self.state_dir))
        except Exception as exc:
            self.connection_status.set("当前连接：" + self.error_text(exc))

    def error_text(self, error):
        message = str(error)
        for secret in self.redact_values:
            if secret:
                message = message.replace(secret, "[已隐藏]")
        return concise_error(message)

    def save_current_connection(self, announce=False):
        if self.busy:
            return False
        key = self.api_key.get().strip()
        if key:
            self.redact_values = tuple(set((*self.redact_values, key)))
        config = {
            "mode": self.selected_mode(),
            "base_url": self.base_url.get().strip(),
            "model": self.model.get().strip(),
            "api_format": self.selected_format(),
        }
        try:
            self.saved_connection = save_connection(self.state_dir, config, api_key=key or None)
        except Exception as exc:
            detail = self.error_text(exc)
            log_error(self.state_dir, detail)
            self.status.set("连接设置未保存：" + detail)
            messagebox.showerror("连接设置未保存", detail, parent=self.root)
            return False
        self.api_key.set("")
        self.mode_changed()
        self.refresh_connection_status()
        if announce:
            self.status.set("连接设置已保存。")
        return True

    def set_busy(self, activity):
        self.activity = activity
        self.busy = activity is not None
        for widget in (self.choose_button, self.scan_button, self.save_button, self.test_button):
            widget.configure(state="disabled" if self.busy else "normal")
        self.mode_box.configure(state="disabled" if self.busy else "readonly")
        self.mode_changed()
        if self.busy:
            self.started_at = time.monotonic()
            self.progress_bar.start(12)
        else:
            self.progress_bar.stop()

    def test_connection(self):
        if not self.save_current_connection():
            return
        self.set_busy("test")
        self.progress_event = {"message": "正在测试连接……"}
        self.render_progress()
        threading.Thread(target=self.connection_worker, args=("test",), daemon=True).start()

    def start_login(self):
        if self.selected_mode() != "chatgpt" or not self.save_current_connection():
            return
        self.set_busy("login")
        self.progress_event = {"message": "正在准备 ChatGPT 登录，请在 Chrome 中完成授权。"}
        self.render_progress()
        threading.Thread(target=self.connection_worker, args=("login",), daemon=True).start()

    def connection_worker(self, operation):
        try:
            if operation == "login":
                result = login_chatgpt(self.state_dir, cli_resolver=find_codex,
                                      on_status=lambda message: self.events.put(("connection_progress", str(message))))
            else:
                result = check_connection(self.state_dir, cli_resolver=find_codex)
            self.events.put(("connection_result", result))
        except Exception as exc:
            detail = self.error_text(exc)
            log_error(self.state_dir, detail)
            self.events.put(("connection_error", detail))

    def choose(self):
        if self.busy:
            return
        initial = self.folder.get()
        options = {"parent": self.root, "title": "选择要检查的 Python 文件夹", "mustexist": True}
        if initial and Path(initial).is_dir():
            options["initialdir"] = initial
        selected = filedialog.askdirectory(**options)
        if not selected:
            return
        try:
            recovering = self.corrupt_state
            if recovering:
                original = (self.state_dir / "state.json").read_bytes()
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = self.state_dir / f"state-corrupt-{stamp}-{uuid.uuid4().hex[:8]}.json"
                with backup.open("xb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
            state = configure_folder(Path(selected), self.state_dir)
        except Exception as exc:
            log_error(self.state_dir, traceback.format_exc())
            messagebox.showerror("无法保存文件夹", str(exc), parent=self.root)
            return
        self.corrupt_state = False
        self.folder.set(state["folder"])
        self.status.set("已记录现有文件，损坏记录已备份。" if recovering else "已记录现有文件，后续将检查新增或内容变化的文件。")

    def scan(self):
        if self.busy:
            return
        folder = self.folder.get()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("文件夹不可用", "请先选择存在的文件夹。", parent=self.root)
            return
        if not self.save_current_connection():
            return
        self.set_busy("scan")
        self.progress_event = {"stage": "scanning"}
        self.render_progress()
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        try:
            ensure_connection(self.state_dir, cli_resolver=find_codex)
        except Exception as exc:
            detail = self.error_text(exc)
            log_error(self.state_dir, detail)
            self.events.put(("connection_error", detail))
            return
        try:
            result = run_scan(self.state_dir, progress=lambda event: self.events.put(("progress", event)))
            self.events.put(("result", result))
        except Exception as exc:
            detail = self.error_text(exc)
            log_error(self.state_dir, detail)
            self.events.put(("error", detail))

    def elapsed_text(self):
        elapsed = max(0, int(time.monotonic() - self.started_at)) if self.started_at is not None else 0
        return f"{elapsed // 60}分{elapsed % 60:02d}秒"

    def render_progress(self):
        event = self.progress_event
        if self.activity in {"test", "login"}:
            self.status.set(f"{event.get('message', '正在连接……')}\n已用时 {self.elapsed_text()}")
            return
        stage = event.get("stage", "scanning")
        filename = Path(str(event.get("filename") or "")).name
        index, total = event.get("index", 0), event.get("total", 0)
        position = f"（{index}/{total}）" if total else ""
        labels = {
            "scanning": "正在检索文件夹……",
            "analyzing": f"正在分析 {filename}{position}",
            "saved": f"已保存 {filename}{position}",
            "opening": f"正在用 VS Code 打开 {filename}{position}",
            "done": "正在汇总检查结果……",
        }
        message = labels.get(stage, "正在检查……")
        waiting = "；仍在等待模型响应。" if stage == "analyzing" and self.started_at is not None and time.monotonic() - self.started_at >= 60 else ""
        self.status.set(f"{message}\n已用时 {self.elapsed_text()}{waiting}")

    def finish(self, kind, payload):
        self.set_busy(None)
        if kind == "connection_result":
            try:
                self.saved_connection = read_connection(self.state_dir)
            except Exception:
                pass
            self.mode_changed()
            self.refresh_connection_status()
            self.status.set(f"{self.error_text(payload)}\n已用时 {self.elapsed_text()}")
        elif kind == "connection_error":
            detail = self.error_text(payload)
            self.status.set(f"连接未完成：{detail}\n已用时 {self.elapsed_text()}")
            if not self.close_when_done:
                self.notebook.select(1)
                messagebox.showerror("连接未完成", detail, parent=self.root)
        elif kind == "result":
            summary = (
                f"新增或变化 {payload.get('detected', 0)} 个，已检查 {payload.get('checked', 0)} 个，"
                f"已修复 {payload.get('fixed', 0)} 个，失败 {payload.get('failed', 0)} 个。"
            )
            errors = payload.get("errors") or []
            for error in errors:
                log_error(self.state_dir, self.error_text(error))
            if payload.get("failed", 0):
                detail = self.error_text(errors[0]) if errors else "未获得具体错误信息，请重新检查。"
                self.status.set(f"{summary}\n{detail}\n已用时 {self.elapsed_text()}")
                if not self.close_when_done:
                    messagebox.showerror("检查未完成", detail, parent=self.root)
            elif not payload.get("detected", 0):
                self.status.set(f"没有新增或内容变化的 .py 文件。已记录且未变化的文件会跳过。\n已用时 {self.elapsed_text()}")
            elif not payload.get("fixed", 0):
                self.status.set(f"{summary}\n检查通过，无需修改，因此未生成副本。\n已用时 {self.elapsed_text()}")
            else:
                self.status.set(f"{summary}\n已用时 {self.elapsed_text()}")
        else:
            detail = self.error_text(payload)
            self.status.set(f"检查失败：{detail}\n已用时 {self.elapsed_text()}")
            if not self.close_when_done:
                messagebox.showerror("检查失败", detail, parent=self.root)
        if self.close_when_done:
            self.root.destroy()

    def poll(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                if isinstance(payload, dict):
                    self.progress_event = payload
                continue
            if kind == "connection_progress":
                self.progress_event = {"message": self.error_text(payload)}
                continue
            self.finish(kind, payload)
            if self.close_when_done:
                return
        if self.busy:
            self.render_progress()
        self.root.after(120, self.poll)

    def close(self):
        if self.busy:
            self.close_when_done = True
            self.root.withdraw()
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="后台检查新增或内容变化的 Python 文件，保留文件名另存到桌面并用 VS Code 打开查看。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--settings", action="store_true", help="打开设置窗口")
    mode.add_argument("--headless", action="store_true", help="完全无窗口运行")
    parser.add_argument("--state-dir", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    state_dir = (args.state_dir or default_state_dir()).resolve()
    try:
        with RunLock(state_dir):
            if args.settings:
                SettingsWindow(state_dir).run()
                return 0
            state = read_state(state_dir)
            folder = state.get("folder")
            if not folder or not Path(folder).is_dir():
                if args.headless:
                    log_error(state_dir, "尚未选择文件夹，或原文件夹不存在。")
                    return 2
                SettingsWindow(state_dir).run()
                return 0
            result = run_scan(state_dir)
            for error in result.get("errors", []):
                log_error(state_dir, str(error))
            return 1 if result.get("failed", 0) else 0
    except AlreadyRunning:
        return 0
    except Exception as exc:
        log_error(state_dir, traceback.format_exc())
        if args.settings:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Python 自动修复", str(exc), parent=root)
            root.destroy()
        return 1


if __name__ == "__main__":
    sys.exit(main())
