# Точка входа плагина: пункт меню «AI Assistant», запуск локального сервера чата и MCP.

import binascii
import os
import tempfile

from PyQt5.QtCore import QObject, QUrl
from PyQt5.QtGui import QDesktopServices

from UM.Application import Application
from UM.Extension import Extension
from UM.Logger import Logger
from UM.Message import Message

from .agent import llm
from .agent.agent import Agent, AgentBusy
from .agent.mainthread import run_on_main
from .agent.mcp import McpHandler
from .agent.registry import ToolRegistry
from .agent.server import ChatServer
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
]


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
        self._registry = ToolRegistry()
        self._tools = SlicerTools(self._output_dir)
        register_tools(self._registry, self._tools)
        self._mcp = McpHandler(self._registry)
        self._agent = Agent(self._registry, self._mcp, self._tools, self._llm_settings,
                            os.path.join(tempfile.gettempdir(), "ste_ai_uploads"))
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
        return self.api_get_config()

    def api_test(self):
        settings = self._llm_settings()
        try:
            models = llm.list_models(settings["base_url"], settings["api_key"], settings["verify_ssl"])
        except llm.LLMError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "models": models[:30],
                "model_found": (not settings["model"]) or settings["model"] in models}

    def api_message(self, text, attachments):
        try:
            self._agent.submit(text, attachments)
        except AgentBusy as e:
            return {"ok": False, "error": str(e)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def api_events(self, after):
        return {"events": self._agent.events.since(after), "busy": self._agent.busy}

    def api_reset(self):
        try:
            self._agent.reset()
        except AgentBusy as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def api_stop(self):
        self._agent.stop()
        return {"ok": True}

    def api_state(self):
        try:
            state = self._tools.get_printer_state()
        except Exception as e:
            return {"error": str(e)}
        state["slice"] = self._tools.get_slice_status()
        return state

    def api_mcp(self, raw):
        return self._mcp.handle_raw(raw)
