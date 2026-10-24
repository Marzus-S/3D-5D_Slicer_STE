# Агент: цикл диалога с LLM. Модель вызывает инструменты, агент выполняет их через MCP-обработчик
# (тот же JSON-RPC, что и внешний /mcp) и возвращает результаты, пока модель не даст итоговый ответ.

import base64
import importlib
import json
import os
import re
import threading
import time
import traceback

from UM.Logger import Logger

from . import knowledge, llm
from .knowledge import MODE_PROMPTS
from .sessions import DEFAULT_TITLE, Session

MAX_STEPS = 32
MAX_TOOL_RESULT_CHARS = 6000
REQUEST_BUDGET = 40000  # байт на запрос к LLM вместе с инструкцией и описанием инструментов
READ_ONLY_TOOLS = {"get_printer_state", "analyze_model", "list_objects", "list_materials", "list_quality_profiles",
                   "search_settings", "get_settings", "get_slice_status", "get_slice_result", "validate_gcode"}
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
        content = knowledge.SYSTEM_PROMPT + "\n\n" + knowledge.MODE_PROMPTS[mode]
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
        try:
            importlib.reload(knowledge)  # правки инструкции подхватываются без перезапуска слайсера
        except Exception as e:
            Logger.log("w", "[AIAssistant] Cannot reload knowledge.py: %s", e)
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
            self._tools.run_context = {"llm_model": model, "mode": mode, "task": _first_user_text(messages)}
            tools_schema = self._registry.llm_tools()
            self._tools.watcher.progress_callback = self._on_progress
            self._tools.cancel_check = lambda: self._cancel
            guard = _LoopGuard()

            for _ in range(MAX_STEPS):
                if guard.tripped:
                    self._events.add("error", text="Агент начал повторять одни и те же действия и остановлен. "
                                                   "Проверьте, что модель прикреплена, или уточните задачу.")
                    messages.append({"role": "assistant", "content": "(остановлено: повтор одних и тех же действий)"})
                    finished_cleanly = True
                    break
                if self._cancel:
                    self._events.add("status", text="Остановлено пользователем")
                    messages.append({"role": "assistant", "content": "(остановлено пользователем)"})
                    finished_cleanly = True
                    break
                self._events.add("thinking")
                message = self._chat(settings, model, mode, messages, tools_schema)
                content = _strip_foreign_script(_text_content(message.get("content")))
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
                        warning = guard.check(call)
                        result_text = json.dumps({"error": warning}, ensure_ascii=False) if warning else self._execute(call)
                        guard.record(call, result_text)
                        hint = guard.decision_hint()
                        if hint and result_text.endswith("}"):
                            result_text = result_text[:-1] + ', "hint": %s}' % json.dumps(hint, ensure_ascii=False)
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
            self._tools.cancel_check = None
            self._tools.run_context = {}  # иначе паспорт, сохранённый через внешний MCP, получит задачу из этого чата
            session.updated = time.time()
            try:
                self._store.save(session)
            except Exception as e:
                Logger.log("w", "[AIAssistant] Cannot save chat: %s", e)
            self._busy = False
            self._running_id = None
            self._events.add("run_end")

    def _chat(self, settings, model, mode, messages, tools_schema):
        """Запрос к LLM в пределах бюджета размера. Сервер организаторов рвёт соединение на больших запросах
        (~50 КБ и больше), поэтому старые результаты инструментов сжимаются; при сбое бюджет уменьшается."""
        tools_size = len(json.dumps(tools_schema, ensure_ascii=False).encode("utf-8"))
        budget = REQUEST_BUDGET
        for attempt in range(1, 4):
            system = self._system_message(mode)
            history = _fit_history(self._history(messages), system, tools_size, budget)
            try:
                return llm.chat(settings["base_url"], settings["api_key"], model,
                                [system] + history, tools_schema,
                                settings["temperature"], settings["verify_ssl"])
            except llm.LLMError as e:
                Logger.log("w", "[AIAssistant] LLM error (attempt %d, budget %d): %s", attempt, budget, e)
                text = str(e).lower()
                too_big = any(w in text for w in ("context", "too long", "token", "length", "1024"))
                if attempt == 3 or not (too_big or e.retryable):
                    raise
                budget = int(budget * 0.7)
                self._events.add("status", text="Нейросеть не ответила — сокращаю запрос и повторяю (%d из 2)" % attempt)
                time.sleep(2 * attempt)

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
        if ok and name in ("export_gcode", "save_project", "save_passport") and isinstance(payload, dict):
            self._events.add("file_saved", kind=name, path=payload.get("path"))
        if ok and name == "apply_plan" and isinstance(payload, dict):
            self._events.add("slice_result", print_time=payload.get("print_time"),
                             weight=payload.get("total_weight_g"), mode=payload.get("printing_mode"))
            self._events.add("file_saved", kind="export_gcode", path=payload.get("gcode_path"))
            self._events.add("file_saved", kind="save_project", path=payload.get("project_path"))
            if payload.get("passport_path"):
                self._events.add("file_saved", kind="save_passport", path=payload.get("passport_path"))
        if ok and name == "analyze_model" and isinstance(payload, dict) and payload.get("preflight"):
            advice = payload.get("five_axis") or {}
            self._events.add("preflight", checks=[{"level": c["level"], "text": c["text"]}
                                                  for c in payload["preflight"]["checks"]],
                             verdict=advice.get("verdict"), score=advice.get("score"),
                             base=advice.get("base_diameter_mm"), blockers=advice.get("blockers") or [],
                             decide=advice.get("decide"))

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


class _LoopGuard:
    """Не даёт модели крутиться на месте: повтор того же вызова и серия неудач одного инструмента."""

    def __init__(self):
        self._last = None
        self._same = 0
        self._seen = {}
        self._reads = 0
        self._fail_tool = None
        self._fails = 0
        self._warnings = 0
        self.tripped = False

    def check(self, call):
        signature = (call["function"]["name"], call["function"]["arguments"])
        # Чтение состояния не меняется, если между вызовами ничего не делали: ловим и повторы через раз.
        if self._seen.get(signature, 0) >= 2 and call["function"]["name"] in READ_ONLY_TOOLS:
            return self._warn("Этот вызов уже выполнялся с теми же аргументами, результат есть выше в истории. "
                              "Не повторяй его — переходи к следующему шагу порядка работы.")
        if signature == self._last and self._same >= 2:
            return self._warn("Этот вызов уже выполнялся несколько раз с теми же аргументами — результат не изменится. "
                              "Не повторяй его: переходи к следующему шагу или объясни пользователю, что мешает.")
        if call["function"]["name"] == self._fail_tool and self._fails >= 3:
            return self._warn("Инструмент %s уже несколько раз подряд вернул ошибку. Не пробуй варианты наугад: "
                              "сообщи пользователю, в чём проблема, и спроси, как быть." % self._fail_tool)
        return None

    def record(self, call, result_text):
        signature = (call["function"]["name"], call["function"]["arguments"])
        self._same = self._same + 1 if signature == self._last else 1
        self._last = signature
        if call["function"]["name"] in READ_ONLY_TOOLS:
            self._seen[signature] = self._seen.get(signature, 0) + 1
            self._reads += 1
        else:
            self._seen = {}  # что-то изменили — повторное чтение снова имеет смысл
            if call["function"]["name"] != "reset_settings":
                self._reads = 0

    def decision_hint(self):
        """Модель долго только читает и ничего не применяет — подсказываем перейти к apply_plan."""
        if self._reads >= 7:
            return ("Данных уже достаточно. Хватит исследовать: прими решение и сразу вызови apply_plan "
                    "(режим, материал, профиль, ориентация, ключевые настройки, name).")
        return None
        failed = result_text.startswith('{"error"')
        if failed:
            name = call["function"]["name"]
            self._fails = self._fails + 1 if name == self._fail_tool else 1
            self._fail_tool = name
        else:
            self._fail_tool, self._fails = None, 0

    def _warn(self, text):
        self._warnings += 1
        if self._warnings >= 3:
            self.tripped = True
        return text


def _fit_history(history, system, tools_size, budget):
    """Сжимает результаты инструментов (кроме двух последних сообщений), пока запрос не влезет в бюджет."""
    def size(items):
        return tools_size + len(json.dumps([system] + items, ensure_ascii=False).encode("utf-8"))

    # Последний раунд (вызовы модели и их результаты) не сжимаем — модель должна видеть их целиком.
    keep = len(history) - 1
    while keep > 0 and not (history[keep].get("role") == "assistant" and history[keep].get("tool_calls")):
        keep -= 1
    keep = keep if keep > 0 else max(len(history) - 2, 0)
    candidate = history
    for limit in (None, 2000, 1000, 500, 250):
        if limit is not None:
            candidate = [_compact(m, limit) if i < keep else m for i, m in enumerate(history)]
        if size(candidate) <= budget:
            return candidate
    # Совсем длинный диалог: отбрасываем старые ходы, начиная новый срез с сообщения пользователя.
    while len(candidate) > 2 and size(candidate) > budget:
        candidate = candidate[1:]
        while candidate and candidate[0].get("role") != "user":
            candidate = candidate[1:]
    return candidate


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


# qwen изредка вставляет в русский текст китайские слова («核心区 под нагруженный профиль»).
_FOREIGN_SCRIPT = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]+")


def _first_user_text(messages):
    """Исходная задача чата (первое сообщение пользователя) — для паспорта; без служебного списка вложений."""
    for message in messages:
        if message.get("role") == "user":
            return _text_content(message.get("content")).split("\n\n[Приложены")[0].strip()
    return ""


def _strip_foreign_script(text):
    return _FOREIGN_SCRIPT.sub("", text) if text else text


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
