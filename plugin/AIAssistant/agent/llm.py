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


def _open(url, api_key, payload=None, timeout=180, verify_ssl=True, accept="application/json"):
    """Открывает HTTP-запрос и переводит сетевые ошибки в понятные LLMError."""
    headers = {"Content-Type": "application/json", "Accept": accept}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context(verify_ssl))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:1500]
        error = LLMError("LLM вернула HTTP %s: %s" % (e.code, detail), retryable=e.code == 429 or e.code >= 500)
        error.status = e.code
        raise error
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            raise LLMError("Не удалось подключиться к LLM: %s (можно отключить проверку SSL в настройках чата)" % reason)
        raise LLMError("Не удалось подключиться к LLM: %s" % reason, retryable=True)
    except OSError as e:
        raise LLMError("Ошибка соединения с LLM: %s" % e, retryable=True)


def _parse_json(body):
    try:
        return json.loads(body)
    except ValueError:
        raise LLMError("LLM вернула не JSON: %s" % body[:500])


def _request(url, api_key, payload=None, timeout=180, verify_ssl=True):
    response = _open(url, api_key, payload, timeout, verify_ssl)
    try:
        with response:
            body = response.read().decode("utf-8", errors="replace")
    except OSError as e:
        raise LLMError("Ошибка соединения с LLM: %s" % e, retryable=True)
    return _parse_json(body)


def list_models(base_url, api_key, verify_ssl=True):
    result = _request(_endpoint(base_url, "/models"), api_key, timeout=30, verify_ssl=verify_ssl)
    return [m.get("id") for m in result.get("data", []) if m.get("id")]


def _message_from_result(result):
    if "error" in result and not result.get("choices"):
        raise LLMError("LLM вернула ошибку: %s" % json.dumps(result["error"], ensure_ascii=False)[:1000])
    choices = result.get("choices") or []
    if not choices:
        raise LLMError("Пустой ответ LLM: %s" % json.dumps(result, ensure_ascii=False)[:500])
    return choices[0].get("message") or {}


def _read_stream(response):
    """Собирает ответ из потока SSE: текст и вызовы инструментов приходят кусочками (delta)."""
    if "text/event-stream" not in (response.headers.get("Content-Type") or ""):
        return _message_from_result(_parse_json(response.read().decode("utf-8", errors="replace")))
    content = []
    calls = {}
    for raw in response:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if chunk.get("error"):
            raise LLMError("LLM вернула ошибку: %s" % json.dumps(chunk["error"], ensure_ascii=False)[:1000])
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for part in delta.get("tool_calls") or []:
                slot = calls.setdefault(part.get("index", len(calls)),
                                        {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    slot["id"] = part["id"]
                function = part.get("function") or {}
                name = function.get("name")
                if name and not slot["function"]["name"].endswith(name):
                    slot["function"]["name"] += name
                arguments = function.get("arguments")
                if arguments:
                    slot["function"]["arguments"] += arguments if isinstance(arguments, str) else json.dumps(arguments)
    message = {"role": "assistant", "content": "".join(content)}
    if calls:
        message["tool_calls"] = [calls[key] for key in sorted(calls)]
    return message


def chat(base_url, api_key, model, messages, tools=None, temperature=0.2, verify_ssl=True, timeout=240, stream=True):
    """Chat Completions. По умолчанию потоком: ответ идёт кусочками, и прокси не рвёт «молчащее» соединение,
    пока модель думает. Если сервер поток не поддерживает (HTTP 400), повторяем обычным запросом."""
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    url = _endpoint(base_url, "/chat/completions")
    if stream:
        payload["stream"] = True
        try:
            response = _open(url, api_key, payload, timeout, verify_ssl, accept="text/event-stream, application/json")
            try:
                with response:
                    return _read_stream(response)
            except OSError as e:
                raise LLMError("Соединение с LLM оборвалось: %s" % e, retryable=True)
        except LLMError as e:
            if getattr(e, "status", None) != 400:
                raise
            del payload["stream"]
    return _message_from_result(_request(url, api_key, payload, timeout=timeout, verify_ssl=verify_ssl))
