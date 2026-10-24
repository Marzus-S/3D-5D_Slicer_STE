# Точка входа плагина: пункт меню «AI Assistant», запуск локального сервера чата и MCP.

import binascii
import json
import os
import tempfile

from PyQt5.QtCore import QObject, QUrl
from PyQt5.QtGui import QDesktopServices

from UM.Application import Application
from UM.Extension import Extension
from UM.Logger import Logger
from UM.Message import Message

from .agent import llm, report
from .agent import commands
from .agent.agent import Agent, AgentBusy
from .agent.journal import Journal, is_error_code
from .agent.learning import LearningTools
from .agent.mainthread import run_on_main
from .agent.mcp import McpHandler
from .agent.recipes import RecipeStore
from .agent.registry import ToolRegistry
from .agent.server import ChatServer
from .agent.sessions import SESSION_ID, SessionStore
from .agent.tools import SlicerTools, register_tools

PREFIX = "aiassistant/"


def _default_output_dir():
    return os.path.join(os.path.expanduser("~"), "Documents", "STE_AI_results")


DEFAULTS = [
    ("base_url", ""),
    ("api_key", ""),
    ("model", ""),
    ("port", 8765),
    ("output_dir", ""),
    ("verify_ssl", True),
    ("temperature", 0.2),
    ("log_user_name", True),
]


def _data_dir():
    """Папка данных слайсера (%APPDATA%\\steslicer\\7.x) — там храним историю чатов."""
    try:
        from UM.Resources import Resources
        return Resources.getDataStoragePath()
    except Exception:
        return tempfile.gettempdir()


def _plugin_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.json"), encoding="utf-8") as stream:
            return json.load(stream).get("version", "")
    except (OSError, ValueError):
        return ""


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


class AIAssistant(QObject, Extension):
    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        Extension.__init__(self)
        self._application = Application.getInstance()
        self._web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

        preferences = self._application.getPreferences()
        for key, default in DEFAULTS:
            preferences.addPreference(PREFIX + key, default)

        self._token = binascii.hexlify(os.urandom(16)).decode("ascii")
        # Журнал для поддержки: user/, model/, errors/ в папке данных слайсера. Секреты вычищаются.
        self.journal = Journal(os.path.join(_data_dir(), "aiassistant_logs"), _plugin_version(),
                               secrets=lambda: [self._pref("api_key") or "", self._token],
                               log_user=lambda: _as_bool(self._pref("log_user_name")))
        self._registry = ToolRegistry()
        self._tools = SlicerTools(self._output_dir)
        self._tools.recipes = RecipeStore(os.path.join(_data_dir(), "aiassistant_recipes.json"))
        self._learning = LearningTools(os.path.join(_data_dir(), "aiassistant_progress.json"))
        register_tools(self._registry, self._tools, self._learning)
        self._mcp = McpHandler(self._registry)
        self._agent = Agent(self._registry, self._mcp, self._tools, self._llm_settings,
                            os.path.join(tempfile.gettempdir(), "ste_ai_uploads"),
                            SessionStore(os.path.join(_data_dir(), "aiassistant_chats")), self.journal, self._learning)
        self._server = ChatServer(self._web_dir, self, self._token)

        self.setMenuName("AI Assistant")
        self.addMenuItem("Открыть чат", self.openChat)
        self.addMenuItem("Адрес MCP-сервера", self.showMcpInfo)

        self._application.engineCreatedSignal.connect(self._ensureServer)

    # ------------------------------------------------------------------ меню

    def _ensureServer(self):
        if self._server.running:
            return True
        try:
            port = int(self._pref("port") or 8765)
        except (TypeError, ValueError):
            port = 8765
        try:
            self._server.start(port)
            return True
        except Exception as e:
            Logger.log("e", "[AIAssistant] Cannot start chat server: %s", e)
            Message(str(e), title="AI Assistant").show()
            return False

    def openChat(self):
        if self._ensureServer():
            QDesktopServices.openUrl(QUrl("%s/#token=%s" % (self._server.base_url, self._token)))

    def showMcpInfo(self):
        if not self._ensureServer():
            return
        text = ("MCP (JSON-RPC 2.0 через HTTP POST): %s/mcp\n"
                "Заголовок: Authorization: Bearer %s\n"
                "Методы: initialize, tools/list, tools/call. Токен меняется при каждом запуске слайсера."
                % (self._server.base_url, self._token))
        Message(text, lifetime=0, title="AI Assistant — MCP-сервер").show()

    # ------------------------------------------------------------------ настройки

    def _pref(self, key):
        return self._application.getPreferences().getValue(PREFIX + key)

    def _output_dir(self):
        return self._pref("output_dir") or _default_output_dir()

    def _llm_settings(self):
        try:
            temperature = float(self._pref("temperature"))
        except (TypeError, ValueError):
            temperature = 0.2
        return {
            "base_url": self._pref("base_url") or "",
            "api_key": self._pref("api_key") or "",
            "model": self._pref("model") or "",
            "temperature": temperature,
            "verify_ssl": _as_bool(self._pref("verify_ssl")),
        }

    # ------------------------------------------------------------------ API для веб-чата (вызывается из потоков сервера)

    def api_get_config(self):
        settings = self._llm_settings()
        return {
            "base_url": settings["base_url"],
            "model": settings["model"],
            "has_key": bool(settings["api_key"]),  # сам ключ наружу не отдаём
            "verify_ssl": settings["verify_ssl"],
            "temperature": settings["temperature"],
            "output_dir": self._output_dir(),
            "mcp_url": self._server.base_url + "/mcp",
            "tools": self._registry.names(),
        }

    def api_set_config(self, body):
        updates = {}
        for key in ("base_url", "model", "output_dir"):
            if key in body:
                updates[key] = str(body[key] or "").strip()
        if body.get("api_key"):
            updates["api_key"] = str(body["api_key"]).strip()
        if "verify_ssl" in body:
            updates["verify_ssl"] = _as_bool(body["verify_ssl"])
        if "temperature" in body:
            updates["temperature"] = float(body["temperature"])

        def apply():
            preferences = self._application.getPreferences()
            for key, value in updates.items():
                preferences.setValue(PREFIX + key, value)
            self._application.savePreferences()

        run_on_main(apply)
        changed = dict((k, v) for k, v in updates.items() if k != "api_key")
        if "api_key" in updates:
            changed["api_key"] = "(заменён)"
        self.journal.record("user", "config_change", changed, session=self._agent.current_id)
        return self.api_get_config()

    def api_test(self):
        settings = self._llm_settings()
        try:
            models = llm.list_models(settings["base_url"], settings["api_key"], settings["verify_ssl"])
        except llm.LLMError as e:
            self.journal.warning("connection_test_failed", {"message": str(e), "status": getattr(e, "status", None),
                                                            "base_url": settings["base_url"]})
            return {"ok": False, "error": str(e)}
        return {"ok": True, "models": models[:30],
                "model_found": (not settings["model"]) or settings["model"] in models}

    def api_message(self, text, attachments, session=None, mode=None):
        try:
            self._agent.submit(text, attachments, session or None, mode or "engineer")
        except (AgentBusy, ValueError) as e:
            self.journal.record("user", "message_rejected", {
                "reason": str(e), "text": text, "attachments": [str(a.get("name")) for a in attachments or []],
            }, session=session or self._agent.current_id, level="warning")
            return {"ok": False, "error": str(e)}
        return {"ok": True, "session": self._agent.current_id}

    def api_commands(self):
        return {"commands": commands.catalog(), "progress": self._learning.score()}

    def api_quiz(self, session, quiz_event, choice):
        try:
            result = self._agent.answer_quiz(session or None, int(quiz_event), str(choice))
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        return dict(result, ok=True)

    def api_events(self, after, session=""):
        session = session if SESSION_ID.match(session or "") else self._agent.current_id
        return {
            "session": session,
            "events": self._agent.events_since(session, after),
            "busy": self._agent.busy,
            "running": self._agent.running_id,
        }

    def api_sessions(self):
        return self._agent.sessions()

    def api_session_new(self):
        session = self._agent.new_session()
        self.journal.record("user", "session_new", session=session)
        return {"ok": True, "session": session}

    def api_session_select(self, session_id):
        try:
            session = self._agent.select(session_id)
            self.journal.record("user", "session_select", session=session)
            return {"ok": True, "session": session}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def api_session_delete(self, session_id):
        try:
            current = self._agent.delete(session_id)
            self.journal.record("user", "session_delete", {"deleted": session_id}, session=current)
            return {"ok": True, "session": current}
        except AgentBusy as e:
            return {"ok": False, "error": str(e)}

    def api_reset(self):
        return {"ok": True, "session": self._agent.reset()}

    def api_stop(self):
        self.journal.record("user", "stop", {"busy": self._agent.busy}, session=self._agent.running_id)
        self._agent.stop()
        return {"ok": True}

    def api_error_report(self, code):
        """HTML-отчёт об ошибке для поддержки; копия сохраняется в errors/reports/."""
        if not is_error_code(code):
            return None
        record = self.journal.find_error(code)
        if record is None:
            return None
        context = {"llm_model": (record.get("data") or {}).get("model") or self._pref("model") or ""}
        try:
            state = self._tools.get_printer_state()
            context.update({"printer": state.get("printer"), "printing_mode": state.get("printing_mode_label")})
        except Exception:
            pass
        page = report.render(record, self.journal.session_events(record.get("session"), record.get("time")), context)
        path = self.journal.report_path(code)
        try:
            if not os.path.isdir(os.path.dirname(path)):
                os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(page)
        except OSError as e:
            Logger.log("w", "[AIAssistant] Cannot save error report: %s", e)
        return page

    def log_server_error(self, method, path, error, traceback_text):
        """Сбой обработчика HTTP-запроса → errors/; код возвращается клиенту вместе с текстом ошибки."""
        return self.journal.error(error, error_type=type(error).__name__, kind="internal", stage="http",
                                  traceback_text=traceback_text, session=self._agent.current_id,
                                  data={"method": method, "path": path})

    def api_state(self):
        try:
            state = self._tools.get_printer_state()
        except Exception as e:
            return {"error": str(e)}
        state["slice"] = self._tools.get_slice_status()
        return state

    def api_mcp(self, raw):
        return self._mcp.handle_raw(raw)
