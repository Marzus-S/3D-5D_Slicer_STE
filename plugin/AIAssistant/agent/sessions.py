# История чатов: каждый диалог — сессия со своими сообщениями для LLM и лентой событий для веб-чата.
# Сессии сохраняются в JSON-файлы в папке данных слайсера и переживают перезапуск.

import binascii
import json
import os
import re
import threading
import time

from UM.Logger import Logger

DEFAULT_TITLE = "Новый чат"
SESSION_ID = re.compile(r"^[0-9a-f]{8,32}$")


class EventLog:
    """Лента событий для веб-чата: пользователь, ответы, вызовы инструментов, прогресс."""

    def __init__(self, events=None, limit=3000):
        self._lock = threading.Lock()
        self._events = list(events or [])
        self._next_id = max([e.get("id", 0) for e in self._events] + [0]) + 1
        self._limit = limit

    def add(self, event_type, **data):
        with self._lock:
            event = {"id": self._next_id, "type": event_type, "time": round(time.time(), 3)}
            event.update(data)
            self._next_id += 1
            self._events.append(event)
            if len(self._events) > self._limit:
                self._events = self._events[-self._limit:]
            return event

    def since(self, after):
        with self._lock:
            return [e for e in self._events if e["id"] > after]

    def all(self):
        with self._lock:
            return list(self._events)


class Session:
    def __init__(self, session_id=None, title=None, created=None, updated=None, messages=None, events=None):
        self.id = session_id or binascii.hexlify(os.urandom(8)).decode("ascii")
        self.title = title or DEFAULT_TITLE
        self.created = created or time.time()
        self.updated = updated or self.created
        self.messages = list(messages or [])
        self.events = EventLog(events)

    def is_empty(self):
        return not self.messages

    def summary(self):
        return {
            "id": self.id,
            "title": self.title,
            "updated": round(self.updated, 3),
            "messages": sum(1 for m in self.messages if m.get("role") == "user"),
        }

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "messages": [_storable(m) for m in self.messages],
            "events": self.events.all(),
        }

    @classmethod
    def from_dict(cls, data):
        return cls(data.get("id"), data.get("title"), data.get("created"), data.get("updated"),
                   data.get("messages"), data.get("events"))


def _storable(message):
    """Фото в историю не пишем — слишком большие; оставляем пометку, что оно было."""
    content = message.get("content")
    if not isinstance(content, list):
        return message
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            parts.append({"type": "text", "text": "[фото — в истории не сохраняется]"})
        else:
            parts.append(part)
    stored = dict(message)
    stored["content"] = parts
    return stored


class SessionStore:
    def __init__(self, directory):
        self._directory = directory

    def _path(self, session_id):
        return os.path.join(self._directory, session_id + ".json")

    def load_all(self):
        sessions = []
        if not os.path.isdir(self._directory):
            return sessions
        for name in os.listdir(self._directory):
            if not name.endswith(".json") or not SESSION_ID.match(name[:-5]):
                continue
            try:
                with open(os.path.join(self._directory, name), "r", encoding="utf-8") as stream:
                    sessions.append(Session.from_dict(json.load(stream)))
            except (OSError, ValueError) as e:
                Logger.log("w", "[AIAssistant] Cannot read chat %s: %s", name, e)
        return sessions

    def save(self, session):
        if not os.path.isdir(self._directory):
            os.makedirs(self._directory)
        path = self._path(session.id)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(session.to_dict(), stream, ensure_ascii=False)
        os.replace(temporary, path)

    def delete(self, session_id):
        try:
            os.remove(self._path(session_id))
        except OSError:
            pass
