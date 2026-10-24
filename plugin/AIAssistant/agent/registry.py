# Реестр инструментов: одно описание — и для MCP (tools/list), и для LLM (tools).

import inspect
import traceback
from collections import OrderedDict

from UM.Logger import Logger


class ToolError(Exception):
    """Ошибка, понятная пользователю и модели (без трейсбека)."""


class Tool:
    def __init__(self, name, title, description, properties, func, required=(), modes=None):
        self.name = name
        self.title = title
        self.description = description
        self.schema = {
            "type": "object",
            "properties": properties,
            "required": list(required),
        }
        self.func = func
        self.modes = set(modes) if modes else None  # режимы чата, в которых модель видит инструмент; None — во всех
        self._accepted = set(inspect.signature(func).parameters.keys())

    def filter_args(self, args):
        return {k: v for k, v in (args or {}).items() if k in self._accepted}


class ToolRegistry:
    def __init__(self):
        self._tools = OrderedDict()

    def add(self, name, title, description, properties, func, required=(), modes=None):
        self._tools[name] = Tool(name, title, description, properties, func, required, modes)

    def has(self, name):
        return name in self._tools

    def title(self, name):
        tool = self._tools.get(name)
        return tool.title if tool else name

    def names(self):
        return list(self._tools.keys())

    def llm_tools(self, mode=None):
        """Описания для LLM. С mode — только инструменты этого режима чата: меньше запрос, меньше соблазнов."""
        return [{
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.schema},
        } for t in self._tools.values() if mode is None or t.modes is None or mode in t.modes]

    def mcp_tools(self):
        return [{
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "inputSchema": t.schema,
        } for t in self._tools.values()]

    def call(self, name, args):
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "result": {"error": "Неизвестный инструмент: %s" % name}}
        if not isinstance(args, dict):
            return {"ok": False, "result": {"error": "Аргументы должны быть JSON-объектом"}}
        missing = [k for k in tool.schema["required"] if k not in args]
        if missing:
            return {"ok": False, "result": {"error": "Не хватает аргументов: %s" % ", ".join(missing)}}
        try:
            result = tool.func(**tool.filter_args(args))
            if result is None:
                result = {"ok": True}
            return {"ok": True, "result": result}
        except ToolError as e:
            return {"ok": False, "result": {"error": str(e)}}
        except Exception as e:
            Logger.log("e", "[AIAssistant] Tool %s failed: %s\n%s", name, e, traceback.format_exc())
            return {"ok": False, "result": {"error": "%s: %s" % (type(e).__name__, e)}}
