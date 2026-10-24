# Клиент OpenAI-совместимого API (Chat Completions + tool calling).
# Только стандартная библиотека: внутри слайсера Python 3.5 и ставить пакеты нельзя.

import json
import ssl
import urllib.error
import urllib.request


class LLMError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable  # временный сбой: перегрузка, таймаут, обрыв связи


def _endpoint(base_url, path):
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LLMError("Не задан URL LLM. Откройте настройки чата и укажите URL и ключ.")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base + path


def _ssl_context(verify_ssl):
    if verify_ssl:
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _request(url, api_key, payload=None, timeout=180, verify_ssl=True):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context(verify_ssl)) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:1500]
        raise LLMError("LLM вернула HTTP %s: %s" % (e.code, detail), retryable=e.code == 429 or e.code >= 500)
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            raise LLMError("Не удалось подключиться к LLM: %s (можно отключить проверку SSL в настройках чата)" % reason)
        raise LLMError("Не удалось подключиться к LLM: %s" % reason, retryable=True)
    except OSError as e:
        raise LLMError("Ошибка соединения с LLM: %s" % e, retryable=True)
    try:
        return json.loads(body)
    except ValueError:
        raise LLMError("LLM вернула не JSON: %s" % body[:500])


def list_models(base_url, api_key, verify_ssl=True):
    result = _request(_endpoint(base_url, "/models"), api_key, timeout=30, verify_ssl=verify_ssl)
    return [m.get("id") for m in result.get("data", []) if m.get("id")]


def chat(base_url, api_key, model, messages, tools=None, temperature=0.2, verify_ssl=True, timeout=240):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    result = _request(_endpoint(base_url, "/chat/completions"), api_key, payload, timeout=timeout, verify_ssl=verify_ssl)
    if "error" in result and not result.get("choices"):
        raise LLMError("LLM вернула ошибку: %s" % json.dumps(result["error"], ensure_ascii=False)[:1000])
    choices = result.get("choices") or []
    if not choices:
        raise LLMError("Пустой ответ LLM: %s" % json.dumps(result, ensure_ascii=False)[:500])
    return choices[0].get("message") or {}
