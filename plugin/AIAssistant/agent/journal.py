# Журнал событий для техподдержки. JSON Lines, один файл в день в каждой из трёх папок:
#   user/    — что прислал пользователь: сообщения, вложения, «Стоп», чаты, смена настроек;
#   model/   — что сделала модель: ответы, вызовы инструментов и их результаты, слайсинг, файлы;
#   errors/  — ошибки с внутренним логом (тип, traceback, контекст) и кодом вида E-1203-7F3A.
# Код ошибки видит пользователь в чате, по нему поддержка находит запись в errors/.
# Ключ API, токен сессии и картинки в журнал не попадают. Ошибка журнала никогда не роняет агента.

import binascii
import getpass
import json
import os
import re
import threading
import time

from UM.Logger import Logger

USER, MODEL, ERRORS = "user", "model", "errors"
USER_EVENTS = ("user",)
MODEL_EVENTS = ("assistant", "assistant_note", "tool_call", "tool_result", "slice_result", "file_saved",
                "status", "run_end")
KEEP_DAYS = 14
MAX_STRING = 4000
HIDDEN_USER = "(скрыто)"

_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{6,}"),
]
_SECRET_KEYS = ("api_key", "apikey", "authorization", "token", "x-auth-token", "password")
_DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
_CODE = re.compile(r"^E-\d{4}-[0-9A-F]{4}$")


def local_time(timestamp=None):
    """ISO-время с часовым поясом. %z в time.strftime на Windows отдаёт имя пояса, поэтому смещение считаем сами."""
    timestamp = time.time() if timestamp is None else timestamp
    local = time.localtime(timestamp)
    offset = -(time.altzone if local.tm_isdst > 0 and time.daylight else time.timezone)
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return "%s.%03d%s%02d%02d" % (time.strftime("%Y-%m-%dT%H:%M:%S", local), int(timestamp * 1000) % 1000,
                                  sign, offset // 3600, offset % 3600 // 60)


def system_user():
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def is_error_code(code):
    return bool(_CODE.match(code or ""))


class Journal:
    def __init__(self, directory, version="", secrets=None, log_user=None, keep_days=KEEP_DAYS):
        self.directory = directory
        self._version = version
        self._secrets = secrets or (lambda: [])     # функция: текущие секреты (ключ API, токен)
        self._log_user = log_user or (lambda: True)  # функция: писать ли логин Windows
        self._keep_days = keep_days
        self._lock = threading.Lock()
        self._user = system_user()
        self._cleanup()

    # ------------------------------------------------------------------ запись

    def record(self, source, event, data=None, session=None, run=None, level="info", error=None):
        entry = {
            "time": local_time(),
            "user": self._user if self._log_user() else HIDDEN_USER,
            "session": session,
            "run": run,
            "event": event,
            "level": level,
            "version": self._version,
            "data": data or {},
        }
        if error is not None:
            entry["error"] = error
        try:
            entry = self._clean(entry, self._current_secrets())
            line = json.dumps(entry, ensure_ascii=False, sort_keys=False)
            path = self._day_path(source)
            with self._lock:
                folder = os.path.dirname(path)
                if not os.path.isdir(folder):
                    os.makedirs(folder)
                with open(path, "a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
        except Exception as e:
            Logger.log("w", "[AIAssistant] Cannot write journal: %s", e)
        return entry

    def chat_event(self, session, run, event):
        """Событие из ленты чата (EventLog) → user/ или model/. Ошибки пишет error(), шум не пишем."""
        kind = event.get("type")
        if kind in USER_EVENTS:
            source = USER
        elif kind in MODEL_EVENTS:
            source = MODEL
        else:
            return
        data = dict((k, v) for k, v in event.items() if k not in ("type", "time"))
        level = "warning" if kind == "tool_result" and not event.get("ok", True) else "info"
        self.record(source, kind, data, session=session, run=run, level=level)

    def error(self, message, error_type="Error", kind="internal", stage="", traceback_text=None,
              session=None, run=None, data=None, level="error"):
        """Пишет ошибку в errors/ и возвращает её код для показа пользователю."""
        code = "E-%s-%s" % (time.strftime("%H%M"), binascii.hexlify(os.urandom(2)).decode("ascii").upper())
        details = {
            "code": code,
            "kind": kind,
            "type": error_type,
            "message": str(message),
            "stage": stage,
        }
        if traceback_text:
            details["traceback"] = traceback_text
        self.record(ERRORS, "error", data, session=session, run=run, level=level, error=details)
        return code

    def warning(self, event, data=None, session=None, run=None):
        """Неопасный сбой (повтор запроса к LLM, ошибка инструмента) — в errors/ без кода."""
        self.record(ERRORS, event, data, session=session, run=run, level="warning")

    # ------------------------------------------------------------------ чтение для отчёта

    def find_error(self, code):
        if not is_error_code(code):
            return None
        for path in reversed(self._day_files(ERRORS)):
            for entry in self._read(path):
                if (entry.get("error") or {}).get("code") == code:
                    return entry
        return None

    def session_events(self, session, before=None, limit=20):
        """Последние события чата из user/ и model/ (до момента ошибки) — для отчёта поддержке."""
        if not session:
            return []
        entries = []
        for source in (USER, MODEL):
            for path in self._day_files(source)[-2:]:
                for entry in self._read(path):
                    if entry.get("session") == session and (before is None or entry.get("time", "") <= before):
                        entry["source"] = source
                        entries.append(entry)
        entries.sort(key=lambda e: e.get("time", ""))
        return entries[-limit:]

    def report_path(self, code):
        return os.path.join(self.directory, ERRORS, "reports", code + ".html")

    # ------------------------------------------------------------------ служебное

    def _day_path(self, source):
        return os.path.join(self.directory, source, time.strftime("%Y-%m-%d") + ".jsonl")

    def _day_files(self, source):
        folder = os.path.join(self.directory, source)
        if not os.path.isdir(folder):
            return []
        return [os.path.join(folder, name) for name in sorted(os.listdir(folder)) if _DAY_FILE.match(name)]

    @staticmethod
    def _read(path):
        entries = []
        try:
            with open(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return entries

    def _current_secrets(self):
        try:
            return [s for s in (self._secrets() or []) if s and len(s) >= 6]
        except Exception:
            return []

    def _clean(self, value, secrets, key=None):
        if key is not None and str(key).lower() in _SECRET_KEYS and value:
            return "***"
        if isinstance(value, dict):
            return dict((k, self._clean(v, secrets, k)) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return [self._clean(v, secrets) for v in value]
        if isinstance(value, str):
            return _redact(value, secrets, limit=None if key == "traceback" else MAX_STRING)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return _redact(str(value), secrets)

    def _cleanup(self):
        limit = time.time() - self._keep_days * 86400
        for folder in (USER, MODEL, ERRORS, os.path.join(ERRORS, "reports")):
            path = os.path.join(self.directory, folder)
            if not os.path.isdir(path):
                continue
            for name in os.listdir(path):
                full = os.path.join(path, name)
                try:
                    if os.path.isfile(full) and os.path.getmtime(full) < limit:
                        os.remove(full)
                except OSError:
                    pass


def _redact(text, secrets, limit=MAX_STRING):
    for secret in secrets:
        text = text.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(0).split()[0] + " ***" if " " in m.group(0) else "sk-***", text)
    if text.startswith("data:") and ";base64," in text[:80]:
        return text.split(",", 1)[0] + ",…(%d символов)" % len(text)
    if limit and len(text) > limit:
        text = text[:limit] + "…(обрезано, всего %d символов)" % len(text)
    return text
