# Агент: цикл диалога с LLM. Модель вызывает инструменты, агент выполняет их через MCP-обработчик
# (тот же JSON-RPC, что и внешний /mcp) и возвращает результаты, пока модель не даст итоговый ответ.

import base64
import json
import os
import re
import threading
import time
import traceback

from UM.Logger import Logger

from . import llm
from .knowledge import SYSTEM_PROMPT

MAX_STEPS = 32
MAX_TOOL_RESULT_CHARS = 9000
HISTORY_LIMIT = 70
MAX_ATTACHMENT_BYTES = 120 * 1024 * 1024
MODEL_EXTENSIONS = (".stl", ".obj", ".3mf")
IMAGE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


class EventLog:
    """Лента событий для веб-чата: пользователь, ответы, вызовы инструментов, прогресс."""

    def __init__(self, limit=3000):
        self._lock = threading.Lock()
        self._events = []
        self._next_id = 1
        self._limit = limit

    def add(self, kind, **data):
        with self._lock:
            event = {"id": self._next_id, "type": kind, "time": round(time.time(), 3)}
            event.update(data)
            self._next_id += 1
            self._events.append(event)
            if len(self._events) > self._limit:
                self._events = self._events[-self._limit:]
            return event

    def since(self, after):
        with self._lock:
            return [e for e in self._events if e["id"] > after]


class AgentBusy(Exception):
    pass


class Agent:
    def __init__(self, registry, mcp_handler, tools, settings_provider, upload_dir):
        self._registry = registry
        self._mcp = mcp_handler
        self._tools = tools
        self._settings_provider = settings_provider
        self._upload_dir = upload_dir
        self.events = EventLog()
        self._messages = []
        self._lock = threading.Lock()
        self._busy = False
        self._cancel = False
        self._request_id = 0
        self._last_progress = (0.0, 0.0)

    @property
    def busy(self):
        return self._busy

    def reset(self):
        if self._busy:
            raise AgentBusy("Агент ещё работает — сначала остановите его")
        self._messages = []
        self.events.add("reset")

    def stop(self):
        if self._busy:
            self._cancel = True

    def submit(self, text, attachments):
        with self._lock:
            if self._busy:
                raise AgentBusy("Агент уже работает над предыдущим запросом")
            self._busy = True
            self._cancel = False
        try:
            files = self._save_attachments(attachments or [])
        except Exception:
            self._busy = False
            raise
        threading.Thread(target=self._run, args=(text or "", files), name="AIAssistantAgent", daemon=True).start()

    # ------------------------------------------------------------------ вложения

    def _save_attachments(self, attachments):
        if not os.path.isdir(self._upload_dir):
            os.makedirs(self._upload_dir)
        saved = []
        for item in attachments:
            name = os.path.basename(str(item.get("name") or "file"))
            extension = os.path.splitext(name)[1].lower()
            if extension not in MODEL_EXTENSIONS and extension not in IMAGE_EXTENSIONS:
                raise ValueError("Неподдерживаемый тип файла: %s (можно STL, OBJ, 3MF, JPG, PNG)" % name)
            data = str(item.get("data") or "")
            if data.startswith("data:"):
                data = data.split(",", 1)[-1]
            raw = base64.b64decode(data)
            if len(raw) > MAX_ATTACHMENT_BYTES:
                raise ValueError("Файл %s слишком большой" % name)
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
            path = os.path.join(self._upload_dir, "%d_%s" % (int(time.time() * 1000), safe))
            with open(path, "wb") as stream:
                stream.write(raw)
            kind = "model" if extension in MODEL_EXTENSIONS else "image"
            saved.append({"name": name, "path": path, "kind": kind, "mime": IMAGE_EXTENSIONS.get(extension)})
        return saved

    @staticmethod
    def _image_data_url(path, mime):
        """Уменьшаем фото до 1280 px (через QImage), чтобы не раздувать запрос к LLM."""
        try:
            from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, Qt
            from PyQt5.QtGui import QImage
            image = QImage(path)
            if not image.isNull():
                if max(image.width(), image.height()) > 1280:
                    image = image.scaled(1280, 1280, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                array = QByteArray()
                buffer = QBuffer(array)
                buffer.open(QIODevice.WriteOnly)
                image.save(buffer, "JPEG", 85)
                buffer.close()
                return "data:image/jpeg;base64," + base64.b64encode(bytes(array)).decode("ascii")
        except Exception:
            pass
        with open(path, "rb") as stream:
            return "data:%s;base64,%s" % (mime or "image/jpeg", base64.b64encode(stream.read()).decode("ascii"))

    def _user_message(self, text, files):
        body = text.strip() or "(без текста)"
        models = [f for f in files if f["kind"] == "model"]
        images = [f for f in files if f["kind"] == "image"]
        if models:
            body += "\n\n[Приложены 3D-модели — загрузи их через load_model]\n" + "\n".join(
                "- %s: %s" % (f["name"], f["path"]) for f in models)
        if images:
            body += "\n\n[Приложены фото: %s]" % ", ".join(f["name"] for f in images)
            content = [{"type": "text", "text": body}]
            for f in images:
                content.append({"type": "image_url", "image_url": {"url": self._image_data_url(f["path"], f["mime"])}})
            return {"role": "user", "content": content}
        return {"role": "user", "content": body}

    # ------------------------------------------------------------------ цикл диалога

    def _system_message(self):
        content = SYSTEM_PROMPT
        try:
            state = self._tools.get_printer_state()
            compact = {
                "printer": state.get("printer"),
                "printing_mode": state.get("printing_mode"),
                "quality": state.get("quality_type"),
                "extruders": [{"index": e["index"], "material": e["material"]} for e in state.get("extruders", [])],
                "objects": [o.get("name") for o in state.get("objects", [])],
            }
            content += "\n\n## Текущее состояние слайсера\n" + json.dumps(compact, ensure_ascii=False)
        except Exception as e:
            Logger.log("w", "[AIAssistant] Cannot read printer state: %s", e)
        return {"role": "system", "content": content}

    def _history(self):
        messages = self._messages
        if len(messages) <= HISTORY_LIMIT:
            return messages
        tail = messages[-HISTORY_LIMIT:]
        while tail and tail[0].get("role") != "user":
            tail = tail[1:]
        return tail

    def _run(self, text, files):
        finished_cleanly = False
        try:
            self.events.add("user", text=text, attachments=[f["name"] for f in files])
            self._messages.append(self._user_message(text, files))
            settings = self._settings_provider()
            model = settings.get("model") or self._auto_model(settings)
            tools_schema = self._registry.llm_tools()
            self._tools.watcher.progress_callback = self._on_progress

            for _ in range(MAX_STEPS):
                if self._cancel:
                    self.events.add("status", text="Остановлено пользователем")
                    self._messages.append({"role": "assistant", "content": "(остановлено пользователем)"})
                    finished_cleanly = True
                    break
                self.events.add("thinking")
                message = llm.chat(settings["base_url"], settings["api_key"], model,
                                   [self._system_message()] + self._history(), tools_schema,
                                   settings["temperature"], settings["verify_ssl"])
                content = _text_content(message.get("content"))
                calls = message.get("tool_calls") or []
                if not calls:
                    calls, content = _inline_tool_calls(content, self._registry)
                if not calls:
                    self._messages.append({"role": "assistant", "content": content})
                    self.events.add("assistant", text=content or "(пустой ответ модели)")
                    finished_cleanly = True
                    break

                normalized = []
                for call in calls:
                    self._request_id += 1
                    function = call.get("function") or {}
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments or {}, ensure_ascii=False)
                    normalized.append({
                        "id": call.get("id") or "call_%d" % self._request_id,
                        "type": "function",
                        "function": {"name": function.get("name") or "", "arguments": arguments},
                    })
                self._messages.append({"role": "assistant", "content": content or "", "tool_calls": normalized})
                if content.strip():
                    self.events.add("assistant_note", text=content)
                for call in normalized:
                    if self._cancel:
                        result_text = json.dumps({"error": "отменено пользователем"}, ensure_ascii=False)
                    else:
                        result_text = self._execute(call)
                    self._messages.append({"role": "tool", "tool_call_id": call["id"], "content": result_text})
            else:
                self.events.add("error", text="Достигнут лимит шагов (%d). Напишите «продолжай», чтобы агент закончил." % MAX_STEPS)
                self._messages.append({"role": "assistant", "content": "(достигнут лимит шагов)"})
                finished_cleanly = True
        except llm.LLMError as e:
            self.events.add("error", text=str(e))
        except Exception as e:
            Logger.log("e", "[AIAssistant] Agent failed: %s\n%s", e, traceback.format_exc())
            self.events.add("error", text="%s: %s" % (type(e).__name__, e))
        finally:
            if not finished_cleanly and self._messages and self._messages[-1].get("role") != "assistant":
                # Роли должны чередоваться — иначе следующий запрос к модели может упасть.
                self._messages.append({"role": "assistant", "content": "(запрос не завершён из-за ошибки)"})
            self._tools.watcher.progress_callback = None
            self._busy = False
            self.events.add("run_end")

    def _auto_model(self, settings):
        models = llm.list_models(settings["base_url"], settings["api_key"], settings["verify_ssl"])
        if not models:
            raise llm.LLMError("Не задана модель LLM, а /models вернул пустой список")
        return models[0]

    def _execute(self, call):
        name = call["function"]["name"]
        title = self._registry.title(name)
        raw = call["function"]["arguments"] or "{}"
        try:
            args = json.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("ожидался объект")
        except ValueError as e:
            payload = {"error": "Аргументы инструмента не являются JSON-объектом: %s" % e}
            self.events.add("tool_call", call_id=call["id"], name=name, title=title, args={"raw": raw[:500]})
            self.events.add("tool_result", call_id=call["id"], name=name, title=title, ok=False,
                            result=_preview(payload), seconds=0)
            return json.dumps(payload, ensure_ascii=False)

        self.events.add("tool_call", call_id=call["id"], name=name, title=title, args=args)
        started = time.time()
        self._request_id += 1
        response = self._mcp.call_tool(name, args, request_id=self._request_id)
        if "error" in response:
            ok, payload = False, {"error": response["error"].get("message")}
        else:
            result = response["result"]
            ok = not result.get("isError")
            payload = result.get("structuredContent")
            if payload is None:
                payload = {"text": result["content"][0]["text"]}
        self.events.add("tool_result", call_id=call["id"], name=name, title=title, ok=ok,
                        result=_preview(payload), seconds=round(time.time() - started, 1))
        if ok and name in ("slice", "get_slice_result") and isinstance(payload, dict) and payload.get("print_time"):
            self.events.add("slice_result", print_time=payload.get("print_time"),
                            weight=payload.get("total_weight_g"), mode=payload.get("printing_mode"))
        if ok and name in ("export_gcode", "save_project") and isinstance(payload, dict):
            self.events.add("file_saved", kind=name, path=payload.get("path"))

        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = text[:MAX_TOOL_RESULT_CHARS] + "…(обрезано)"
        return text

    def _on_progress(self, amount):
        now = time.time()
        last_amount, last_time = self._last_progress
        if amount >= 1.0 or abs(amount - last_amount) >= 0.05 or now - last_time > 2.0:
            self._last_progress = (amount, now)
            self.events.add("progress", value=round(amount, 3))


def _text_content(content):
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


_INLINE_PATTERNS = [
    re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S),
    re.compile(r"```(?:json|tool_code|tool_call)?\s*(\{.*?\}|\[.*?\])\s*```", re.S),
]


def _inline_tool_calls(content, registry):
    """Некоторые модели (в т.ч. семейства Gemma) иногда пишут вызов инструмента текстом, а не в tool_calls."""
    if not content or "{" not in content:
        return [], content
    candidates = []
    for pattern in _INLINE_PATTERNS:
        candidates.extend(pattern.findall(content))
    stripped = content.strip()
    if not candidates and stripped.startswith(("{", "[")):
        candidates.append(stripped)
    calls = []
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            name = item.get("name") or (item.get("function") or {}).get("name")
            arguments = item.get("arguments", item.get("parameters", {}))
            if name and registry.has(name):
                calls.append({"function": {"name": name, "arguments": arguments}})
    if not calls:
        return [], content
    remaining = content
    for pattern in _INLINE_PATTERNS:
        remaining = pattern.sub("", remaining)
    if remaining.strip().startswith(("{", "[")):
        remaining = ""
    return calls, remaining.strip()


def _preview(payload, limit=2500):
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    return text if len(text) <= limit else text[:limit] + "\n…"
