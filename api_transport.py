import json
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 6_144_000
MAX_ERROR_BYTES = 65_536
DEFAULT_TIMEOUT = 180


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def build_api_url(config):
    mode = config.get("mode")
    if mode not in {"openai_api", "custom_api"}:
        raise ValueError("请选择官方 API 或自定义 API 连接。")
    api_format = config.get("api_format", "responses")
    if api_format not in {"responses", "chat_completions"}:
        raise ValueError("API 协议必须选择 Responses 或 Chat Completions。")
    base = "https://api.openai.com/v1" if mode == "openai_api" else config.get("base_url", "")
    if not isinstance(base, str) or not base.strip():
        raise ValueError("请填写 API 地址。")
    base = base.strip()
    if re.search(r"[\s\x00-\x1f\x7f]", base):
        raise ValueError("API 地址不能含空白或控制字符。")
    try:
        parts = urllib.parse.urlsplit(base)
        valid = (parts.scheme in {"http", "https"} and bool(parts.hostname)
                 and parts.username is None and parts.password is None
                 and not parts.query and not parts.fragment)
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("API 地址须为完整的 HTTP 或 HTTPS 地址，且不能含账号、查询参数或片段。")
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    endpoint = "/responses" if api_format == "responses" else "/chat/completions"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path + endpoint, "", ""))


def _error_message(status, detail):
    text = detail.lower()
    if status in {401, 403} or any(code in text for code in ("invalid_api_key", "invalid_authentication", "authentication_error")):
        return "API 认证失败或无访问权限，请检查密钥和服务商授权。"
    if status == 429 or any(code in text for code in ("rate_limit", "insufficient_quota", "usage_limit")):
        return "API 使用额度或请求频率受到限制，请检查额度或稍后重试。"
    if "model_not_found" in text or "unsupported_model" in text:
        return "API 无法使用所选模型，请检查服务商提供的模型名称和权限。"
    if 300 <= status < 400:
        return "API 地址发生重定向，请填写服务商的最终接口地址。"
    if status in {404, 405}:
        return "API 接口或模型不存在，请检查地址、协议和模型名称。"
    if status in {400, 415, 422}:
        return "API 请求不被支持，请检查所选协议和模型能力。"
    if status >= 500:
        return "API 服务暂时不可用，请稍后重试。"
    return f"API 调用失败（HTTP {status}），请检查连接设置。"


def _schema_not_supported(status, detail):
    text = detail.lower()
    if status not in {400, 422} or any(code in text for code in (
        "invalid_api_key", "invalid_authentication", "authentication_error", "rate_limit", "insufficient_quota", "model_not_found",
    )):
        return False
    return (re.search(r"response_format|json_schema|text\.format|structured.?outputs?", text) is not None
            and re.search(r"not supported|unsupported|does not support|unknown|unrecognized|unexpected", text) is not None)


def _extract_response_text(value, api_format):
    if not isinstance(value, dict):
        raise ValueError("API 返回格式无效。")
    if value.get("error"):
        raise ValueError("API 返回了错误，未取得完整的分析结果。")
    if api_format == "chat_completions":
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("API 未返回可用的分析结果。")
        choice = choices[0]
        if choice.get("finish_reason") not in {None, "stop"}:
            raise ValueError("API 分析未完整结束，未保存修复结果。")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("tool_calls") or message.get("function_call"):
            raise ValueError("API 返回了工具调用，未保存修复结果。")
        if message.get("refusal"):
            raise ValueError("API 拒绝了本次分析，未保存修复结果。")
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for item in content:
                if not isinstance(item, dict) or item.get("type") not in {"text", "output_text"} or not isinstance(item.get("text"), str):
                    raise ValueError("API 返回了无法使用的分析内容。")
                chunks.append(item["text"])
            return "".join(chunks)
        raise ValueError("API 未返回文本分析结果。")
    if value.get("status") not in {None, "completed"} or value.get("incomplete_details"):
        raise ValueError("API 分析未完整结束，未保存修复结果。")
    output = value.get("output")
    if not isinstance(output, list):
        raise ValueError("API 未返回可用的分析结果。")
    chunks = []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("API 返回格式无效。")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message":
            raise ValueError("API 返回了工具调用或非文本内容，未保存修复结果。")
        if item.get("status") not in {None, "completed"}:
            raise ValueError("API 分析未完整结束，未保存修复结果。")
        content = item.get("content")
        if not isinstance(content, list):
            raise ValueError("API 返回格式无效。")
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise ValueError("API 拒绝分析或返回了非文本内容，未保存修复结果。")
            chunks.append(part["text"])
    return "".join(chunks)


def _decode_result(text):
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n?```", text, re.I)
    if fenced:
        text = fenced[1]
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        raise ValueError("API 未返回有效的 JSON 修复结果。") from None
    if not isinstance(result, dict):
        raise ValueError("API 返回的修复结果必须为 JSON 对象。")
    return result


def _request_result(instruction, schema, config, api_key, deadline):
    url = build_api_url(config)
    api_format = config.get("api_format", "responses")
    model = config.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or re.search(r"[\x00-\x1f\x7f]", model):
        raise ValueError("请填写服务商提供的有效模型名称。")
    if not isinstance(api_key, str) or not api_key.strip() or re.search(r"[\x00-\x1f\x7f]", api_key):
        raise ValueError("请填写有效的 API 密钥。")
    if api_format == "responses":
        payload = {
            "model": model.strip(), "input": instruction, "store": False,
            "text": {"format": {"type": "json_schema", "name": "repair", "strict": True, "schema": schema}},
        }
    else:
        payload = {
            "model": model.strip(),
            "messages": [
                {"role": "system", "content": "Return only a JSON object with has_errors (boolean) and corrected_code (string). Follow the supplied review rules. Never call tools."},
                {"role": "user", "content": instruction},
            ],
            "response_format": {"type": "json_schema", "json_schema": {"name": "repair", "strict": True, "schema": schema}},
        }
    opener = urllib.request.build_opener(_NoRedirect())
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("API 分析超时，下次启动将重试。")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={
            "Authorization": "Bearer " + api_key.strip(),
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "PythonAutoRepair/2",
        }, method="POST")
        try:
            with opener.open(request, timeout=remaining) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("API 返回内容过大，未保存修复结果。")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(MAX_ERROR_BYTES).decode("utf-8", errors="replace")
            except (OSError, ValueError):
                detail = ""
            finally:
                exc.close()
            if attempt == 0 and _schema_not_supported(exc.code, detail) and time.monotonic() < deadline:
                payload.pop("response_format", None)
                payload.pop("text", None)
                continue
            raise RuntimeError(_error_message(exc.code, detail)) from None
        except (TimeoutError, socket.timeout):
            raise TimeoutError("API 分析超时，下次启动将重试。") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc) == "API 返回内容过大，未保存修复结果。":
                raise
            raise RuntimeError("API 网络连接失败，请检查网络和连接设置。") from None
        try:
            value = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            raise ValueError("API 返回了无效的 JSON 响应。") from None
        return _decode_result(_extract_response_text(value, api_format))
    raise RuntimeError("API 请求不被支持，请检查所选协议和模型能力。")


def analyze_api(instruction, schema, config, api_key):
    try:
        timeout = float(config.get("timeout", DEFAULT_TIMEOUT))
    except (ValueError, TypeError):
        raise ValueError("API 超时设置无效。") from None
    if not 0 < timeout <= DEFAULT_TIMEOUT:
        raise ValueError("API 超时须在 0 到 180 秒之间。")
    deadline = time.monotonic() + timeout
    completed = queue.Queue(maxsize=1)

    def worker():
        try:
            completed.put((True, _request_result(instruction, schema, config, api_key, deadline)))
        except Exception as exc:
            if not isinstance(exc, (RuntimeError, ValueError, TimeoutError)):
                exc = RuntimeError("API 调用失败，请检查连接设置。")
            completed.put((False, exc))

    threading.Thread(target=worker, name="repair-api-request", daemon=True).start()
    try:
        succeeded, result = completed.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise TimeoutError("API 分析超时，下次启动将重试。") from None
    if not succeeded:
        raise result from None
    return result
