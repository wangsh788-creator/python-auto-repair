import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid


MODES = {"chatgpt", "openai_api", "custom_api"}
FORMATS = {"responses", "chat_completions"}
OFFICIAL_URL = "https://api.openai.com/v1"
SECRET_ENVIRONMENT = {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL",
                      "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "CODEX_THREAD_ID",
                      "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_MANAGED_BY_NPM"}


def _defaults():
    return {"version": 1, "mode": "chatgpt", "base_url": OFFICIAL_URL, "model": "",
            "api_format": "responses", "has_api_key": False}


def _document(state_dir):
    path = Path(state_dir) / "connection.json"
    if not path.is_file():
        return {}
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError()
        return document
    except (ValueError, OSError, UnicodeError):
        raise RuntimeError("无法读取模型连接设置，请在设置窗口重新保存连接。") from None


def _normalize(config):
    result = _defaults()
    result.update({key: config[key] for key in ("mode", "base_url", "model", "api_format") if key in config})
    if result["mode"] not in MODES or result["api_format"] not in FORMATS:
        raise ValueError("请选择有效的连接方式和接口协议。")
    if not isinstance(result["model"], str):
        raise ValueError("模型名称格式错误。")
    result["model"] = result["model"].strip()
    if result["model"] and not re.fullmatch(r"[\w./:@+\-]{1,200}", result["model"]):
        raise ValueError("模型名称只能包含文字、数字、下划线和常见路径符号。")
    if result["mode"] == "chatgpt":
        result["base_url"] = OFFICIAL_URL
        result["api_format"] = "responses"
        return result
    if not result["model"]:
        raise ValueError("请填写服务商提供的模型名称。")
    if result["mode"] == "openai_api":
        result["base_url"] = OFFICIAL_URL
    if not isinstance(result["base_url"], str):
        raise ValueError("API 地址格式错误。")
    result["base_url"] = result["base_url"].strip().rstrip("/")
    try:
        url = urlsplit(result["base_url"])
        url.port
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username is not None or url.password is not None or url.query or url.fragment
                or re.search(r"[\s\x00-\x1f\x7f]", result["base_url"])):
            raise ValueError()
    except ValueError:
        raise ValueError("请填写完整的 HTTP 或 HTTPS API 地址。") from None
    return result


def read_connection(state_dir):
    document = _document(state_dir)
    result = _normalize(document) if document else _defaults()
    result["has_api_key"] = bool(document.get("api_key_encrypted"))
    return result


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(raw, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("此版本的密钥保存功能需要 Windows。")
    buffer = ctypes.create_string_buffer(raw)
    incoming = _Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise RuntimeError("无法读取本机保存的密钥，请在连接设置中重新填写。" if decrypt else "无法安全保存密钥。")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel32.LocalFree(outgoing.data)


def get_api_key(state_dir):
    encoded = _document(state_dir).get("api_key_encrypted")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("请在连接设置中填写 API 密钥并保存。")
    try:
        return _protect(base64.b64decode(encoded, validate=True), decrypt=True).decode("utf-8")
    except (ValueError, UnicodeError):
        raise RuntimeError("保存的密钥无法读取，请重新填写。") from None


def save_connection(state_dir, config, api_key=None):
    result = _normalize(config)
    try:
        previous = _document(state_dir)
    except RuntimeError:
        previous = {}
    encrypted = ""
    if result["mode"] != "chatgpt":
        if api_key:
            if not isinstance(api_key, str) or len(api_key) > 8192 or any(c.isspace() for c in api_key):
                raise ValueError("API 密钥格式错误。")
            encrypted = base64.b64encode(_protect(api_key.encode("utf-8"))).decode("ascii")
        elif previous.get("mode") == result["mode"] and previous.get("base_url") == result["base_url"]:
            encrypted = previous.get("api_key_encrypted", "")
        if not encrypted:
            raise ValueError("请填写此连接的 API 密钥；切换服务商或地址后需要重新填写。")
    document = {key: value for key, value in result.items() if key != "has_api_key"}
    document["api_key_encrypted"] = encrypted
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ("connection-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "connection.json")
    finally:
        temporary.unlink(missing_ok=True)
    return read_connection(state_dir)


def connection_summary(state_dir):
    config = read_connection(state_dir)
    names = {"chatgpt": "ChatGPT 登录 / Codex", "openai_api": "官方 API", "custom_api": "自定义 API"}
    return names[config["mode"]] + (" · " + config["model"] if config["model"] else " · 默认模型")


def hidden_options():
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def codex_environment(state_dir):
    environment = os.environ.copy()
    for key in SECRET_ENVIRONMENT:
        environment.pop(key, None)
    login_home = Path(state_dir) / "codex-home"
    if (login_home / "auth.json").is_file():
        environment["CODEX_HOME"] = str(login_home)
    return environment


def _resolve_cli(resolver):
    if resolver is None:
        from core import find_codex
        resolver = find_codex
    return resolver()


def ensure_connection(state_dir, cli_resolver=None):
    config = read_connection(state_dir)
    if config["mode"] != "chatgpt":
        get_api_key(state_dir)
        return
    cli = _resolve_cli(cli_resolver)
    environment = codex_environment(state_dir)
    try:
        status = subprocess.run([str(cli), "login", "status"], env=environment, capture_output=True,
                                timeout=15, **hidden_options())
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("无法检查 ChatGPT 登录，请在模型连接中重新登录。") from None
    text = (status.stdout + status.stderr).decode("utf-8", errors="replace").lower()
    if status.returncode or not re.search(r"logged in (?:using|with) chatgpt", text):
        raise RuntimeError("尚未使用 ChatGPT 登录，请打开模型连接并点击“ChatGPT 登录（Chrome）”。")


def check_connection(state_dir, cli_resolver=None):
    ensure_connection(state_dir, cli_resolver)
    from core import analyze_with_backend, validate_response
    source = "value = 1\n"
    response = analyze_with_backend(source, "connection_check.py", state_dir)
    validate_response(source, response)
    return "连接测试成功，模型已正常返回结果。"


def _chrome():
    candidates = []
    if os.environ.get("PY_AUTO_REPAIR_CHROME"):
        candidates.append(Path(os.environ["PY_AUTO_REPAIR_CHROME"]))
    for variable, fallback in (("ProgramFiles", r"C:\Program Files"),
                               ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                               ("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))):
        candidates.append(Path(os.environ.get(variable, fallback)) / "Google" / "Chrome" / "Application" / "chrome.exe")
    command = shutil.which("chrome.exe")
    if command:
        candidates.append(Path(command))
    for path in candidates:
        if path.is_file() and path.suffix.lower() == ".exe":
            return path.resolve()
    raise FileNotFoundError("未找到 Chrome，请先安装 Chrome 后再登录。")


def _stop_process(process):
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait(timeout=5)


def _external_process(command, **kwargs):
    previous = None
    if os.name == "nt" and getattr(sys, "frozen", False):
        buffer = ctypes.create_unicode_buffer(32768)
        ctypes.windll.kernel32.GetDllDirectoryW(len(buffer), buffer)
        previous = buffer.value
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(command, **kwargs)
    finally:
        if previous is not None:
            ctypes.windll.kernel32.SetDllDirectoryW(previous or None)


def login_chatgpt(state_dir, cli_resolver=None, on_status=None):
    chrome = _chrome()
    cli = _resolve_cli(cli_resolver)
    login_home = Path(state_dir) / "codex-home"
    login_home.mkdir(parents=True, exist_ok=True)
    environment = codex_environment(state_dir)
    environment["CODEX_HOME"] = str(login_home)
    process = _external_process([str(cli), "app-server", "--stdio", "-c", 'cli_auth_credentials_store="file"'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=environment, text=True, encoding="utf-8", **hidden_options())
    messages = queue.Queue()

    def receive():
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    messages.put(message)
        finally:
            messages.put(None)

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    auth_requested = False
    deadline = time.monotonic() + 60
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "python_auto_repair", "title": "Python Auto Repair", "version": "2.0"},
            "capabilities": {"explicitGatewayOauth": True}}})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ChatGPT 登录等待超时，请重新发起授权。")
            try:
                message = messages.get(timeout=min(1, remaining))
            except queue.Empty:
                continue
            if message is None:
                raise RuntimeError("ChatGPT 登录服务已退出，请重试。")
            if message.get("id") == 1:
                if "error" in message:
                    raise RuntimeError("无法启动 ChatGPT 授权，请更新应用后重试。")
                send({"method": "initialized", "params": {}})
                send({"id": 2, "method": "account/login/start", "params": {"type": "chatgpt"}})
            elif message.get("id") == 2:
                url = message.get("result", {}).get("authUrl", "")
                parsed = urlsplit(url)
                if ("error" in message or parsed.scheme != "https"
                        or parsed.hostname not in {"auth.openai.com", "chatgpt.com"} or parsed.username or parsed.password):
                    raise RuntimeError("无法取得 ChatGPT 授权链接，请稍后重试。")
                _external_process([str(chrome), "--new-tab", url], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, **hidden_options())
                auth_requested = True
                deadline = time.monotonic() + 600
                if on_status is not None:
                    on_status("已在 Chrome 打开授权页，请在浏览器完成 ChatGPT 登录和授权。")
            elif message.get("method") == "account/login/completed":
                if not auth_requested or not message.get("params", {}).get("success"):
                    raise RuntimeError("ChatGPT 授权未完成，请重新登录。")
                ensure_connection(state_dir, lambda: cli)
                return "ChatGPT 登录成功。"
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError("ChatGPT 登录连接失败，请检查网络后重试。") from None
    finally:
        _stop_process(process)
        reader.join(timeout=2)
        process.stdin.close()
        process.stdout.close()
