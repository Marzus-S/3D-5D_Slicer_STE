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
from .knowledge import MODE_PROMPTS, SYSTEM_PROMPT
from .sessions import DEFAULT_TITLE, Session

MAX_STEPS = 32
MAX_TOOL_RESULT_CHARS = 9000
HISTORY_LIMIT = 70
MAX_ATTACHMENT_BYTES = 120 * 1024 * 1024
MODEL_EXTENSIONS = (".stl", ".obj", ".3mf")
IMAGE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


class AgentBusy(Exception):
    pass


class Agent:
    def __init__(self, registry, mcp_handler, tools, settings_provider, upload_dir, store):
        self._registry = registry
        self._mcp = mcp_handler
        self._tools = tools
        self._settings_provider = settings_provider
        self._upload_dir = upload_dir
        self._store = store
        self._sessions = {s.id: s for s in store.load_all()}
        recent = sorted(self._sessions.values(), key=lambda s: s.updated, reverse=True)
        self._current_id = recent[0].id if recent else None
        self._running_id = None
        self._events = None  # лента событий чата, в котором сейчас идёт работа
        self._lock = threading.Lock()
        self._busy = False
        self._cancel = False
        self._request_id = 0
        self._last_progress = (0.0, 0.0)

    @property
    def busy(self):
        return self._busy

    @property
    def running_id(self):
        return self._running_id

    @property
    def current_id(self):
        return self._current().id

    # ------------------------------------------------------------------ история чатов

    def _current(self):
        session = self._sessions.get(self._current_id)
        if session is None:
            session = Session()
            self._sessions[session.id] = session
            self._current_id = session.id
        return session

    def sessions(self):
        current = self._current()
        items = sorted(self._sessions.values(), key=lambda s: s.updated, reverse=True)
        return {
            "sessions": [s.summary() for s in items if not s.is_empty() or s.id == current.id],
            "current": current.id,
            "busy": self._busy,
            "running": self._running_id,
        }

    def new_session(self):
        current = self._current()
        if current.is_empty():
            return current.id
        session = Session()
        self._sessions[session.id] = session
        self._current_id = session.id
        return session.id

    def select(self, session_id):
        if session_id not in self._sessions:
            raise ValueError("Чат не найден")
        self._current_id = session_id
        return session_id

    def delete(self, session_id):
        if self._busy and self._running_id == session_id:
            raise AgentBusy("В этом чате агент ещё работает — сначала остановите его")
        self._sessions.pop(session_id, None)
        self._store.delete(session_id)
        if self._current_id == session_id:
            recent = sorted(self._sessions.values(), key=lambda s: s.updated, reverse=True)
            self._current_id = recent[0].id if recent else None
        return self._current().id

    def events_since(self, session_id, after):
        session = self._sessions.get(session_id) if session_id else self._current()
        return session.events.since(after) if session else []

    def reset(self):
        """Старое «Новый диалог» — теперь просто открывает новый чат, старые остаются в истории."""
        return self.new_session()

    def stop(self):
        if self._busy:
            self._cancel = True

    def submit(self, text, attachments, session_id=None, mode="engineer"):
        with self._lock:
            if self._busy:
                raise AgentBusy("Агент уже работает над предыдущим запросом")
            self._busy = True
            self._cancel = False
        try:
            session = self._sessions.get(session_id) or self._current()
            self._current_id = session.id
            files = self._save_attachments(attachments or [])
        except Exception:
            self._busy = False
            raise
        self._running_id = session.id
        mode = mode if mode in MODE_PROMPTS else "engineer"
        threading.Thread(target=self._run, args=(session, text or "", files, mode),
                         name="AIAssistantAgent", daemon=True).start()

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

    def _system_message(self, mode):
        content = SYSTEM_PROMPT + "\n\n" + MODE_PROMPTS[mode]
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

    @staticmethod
    def _history(messages):
        if len(messages) <= HISTORY_LIMIT:
            return messages
        tail = messages[-HISTORY_LIMIT:]
        while tail and tail[0].get("role") != "user":
            tail = tail[1:]
        return tail

    def _run(self, session, text, files, mode):
        messages = session.messages
        self._events = session.events
        finished_cleanly = False
        try:
            if session.title == DEFAULT_TITLE:
                title = " ".join(text.split())[:60]
                if not title and files:
                    title = "Модель " + files[0]["name"]
                session.title = title or DEFAULT_TITLE
            self._events.add("user", text=text, attachments=[f["name"] for f in files], mode=mode)
            messages.append(self._user_message(text, files))
            settings = self._settings_provider()
            model = settings.get("model") or self._auto_model(settings)
            tools_schema = self._registry.llm_tools()
            self._tools.watcher.progress_callback = self._on_progress

            for _ in range(MAX_STEPS):
                if self._cancel:
                    self._events.add("status", text="Остановлено пользователем")
                    messages.append({"role": "assistant", "content": "(остановлено пользователем)"})
                    finished_cleanly = True
                    break
                self._events.add("thinking")
                message = self._chat(settings, model, mode, messages, tools_schema)
                content = _text_content(message.get("content"))
                calls = message.get("tool_calls") or []
                if not calls:
                    calls, content = _inline_tool_calls(content, self._registry)
                if not calls:
                    messages.append({"role": "assistant", "content": content})
                    self._events.add("assistant", text=content or "(пустой ответ модели)")
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
                messages.append({"role": "assistant", "content": content or "", "tool_calls": normalized})
                if content.strip():
                    self._events.add("assistant_note", text=content)
                for call in normalized:
                    if self._cancel:
                        result_text = json.dumps({"error": "отменено пользователем"}, ensure_ascii=False)
                    else:
                        result_text = self._execute(call)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": result_text})
            else:
                self._events.add("error", text="Достигнут лимит шагов (%d). Напишите «продолжай», чтобы агент закончил." % MAX_STEPS)
                messages.append({"role": "assistant", "content": "(достигнут лимит шагов)"})
                finished_cleanly = True
        except llm.LLMError as e:
            Logger.log("e", "[AIAssistant] LLM error: %s", e)
            self._events.add("error", text=str(e))
        except Exception as e:
            Logger.log("e", "[AIAssistant] Agent failed: %s\n%s", e, traceback.format_exc())
            self._events.add("error", text="%s: %s" % (type(e).__name__, e))
        finally:
            if not finished_cleanly and messages and messages[-1].get("role") != "assistant":
                # Роли должны чередоваться — иначе следующий запрос к модели может упасть.
                messages.append({"role": "assistant", "content": "(запрос не завершён из-за ошибки)"})
            self._tools.watcher.progress_callback = None
            session.updated = time.time()
            try:
                self._store.save(session)
            except Exception as e:
                Logger.log("w", "[AIAssistant] Cannot save chat: %s", e)
            self._busy = False
            self._running_id = None
            self._events.add("run_end")

    def _chat(self, settings, model, mode, messages, tools_schema):
        """Запрос к LLM с повтором: временные сбои — до 2 повторов, слишком длинный запрос — сжимаем историю."""
        compact = False
        for attempt in range(1, 4):
            history = self._history(messages)
            if compact:
                history = [_compact(m) for m in history]
            try:
                return llm.chat(settings["base_url"], settings["api_key"], model,
                                [self._system_message(mode)] + history, tools_schema,
                                settings["temperature"], settings["verify_ssl"])
            except llm.LLMError as e:
                Logger.log("w", "[AIAssistant] LLM error (attempt %d): %s", attempt, e)
                text = str(e).lower()
                if attempt == 3:
                    raise
                if not compact and any(w in text for w in ("context", "too long", "token", "length", "1024")):
                    compact = True
                    self._events.add("status", text="Запрос к нейросети слишком большой — сокращаю историю и повторяю")
                elif e.retryable:
                    self._events.add("status", text="Нейросеть не ответила — повторяю запрос (%d из 2)" % attempt)
                    time.sleep(3 * attempt)
                else:
                    raise

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
            self._events.add("tool_call", call_id=call["id"], name=name, title=title, args={"raw": raw[:500]})
            self._events.add("tool_result", call_id=call["id"], name=name, title=title, ok=False,
                            result=_preview(payload), seconds=0)
            return json.dumps(payload, ensure_ascii=False)

        self._events.add("tool_call", call_id=call["id"], name=name, title=title, args=args)
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
        self._events.add("tool_result", call_id=call["id"], name=name, title=title, ok=ok,
                        result=_preview(payload), seconds=round(time.time() - started, 1))
        if ok and name in ("slice", "get_slice_result") and isinstance(payload, dict) and payload.get("print_time"):
            self._events.add("slice_result", print_time=payload.get("print_time"),
                            weight=payload.get("total_weight_g"), mode=payload.get("printing_mode"))
        if ok and name in ("export_gcode", "save_project") and isinstance(payload, dict):
            self._events.add("file_saved", kind=name, path=payload.get("path"))

        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = text[:MAX_TOOL_RESULT_CHARS] + "…(обрезано)"
        return text

    def _on_progress(self, amount):
        now = time.time()
        last_amount, last_time = self._last_progress
        if amount >= 1.0 or abs(amount - last_amount) >= 0.05 or now - last_time > 2.0:
            self._last_progress = (amount, now)
            self._events.add("progress", value=round(amount, 3))


def _compact(message, limit=1500):
    """Укорачивает длинные результаты инструментов, чтобы запрос влез в лимит модели."""
    content = message.get("content")
    if message.get("role") == "tool" and isinstance(content, str) and len(content) > limit:
        shorter = dict(message)
        shorter["content"] = content[:limit] + "…(сокращено)"
        return shorter
    return message


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
