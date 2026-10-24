# Минимальный MCP-сервер: JSON-RPC 2.0, методы initialize, ping, tools/list, tools/call.
# Официальный MCP SDK требует Python 3.10+, поэтому протокол реализован вручную.
# Этот же обработчик использует и встроенный агент — он ходит к инструментам как MCP-клиент.

import json

SERVER_INFO = {"name": "ste-slicer-ai-assistant", "version": "0.1.0"}
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class McpHandler:
    def __init__(self, registry):
        self._registry = registry

    def handle_raw(self, body):
        """Принимает тело HTTP-запроса (str), возвращает строку ответа или None (для уведомлений)."""
        try:
            payload = json.loads(body)
        except ValueError:
            return json.dumps(_error(None, PARSE_ERROR, "Parse error"))
        response = self.handle(payload)
        if response is None:
            return None
        return json.dumps(response, ensure_ascii=False)

    def handle(self, payload):
        if isinstance(payload, list):
            responses = [r for r in (self._handle_one(item) for item in payload) if r is not None]
            return responses or None
        return self._handle_one(payload)

    def call_tool(self, name, arguments, request_id=1):
        """Удобная обёртка для встроенного агента: tools/call через тот же JSON-RPC."""
        return self._handle_one({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    def _handle_one(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "Invalid Request")

        method = message["method"]
        request_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": "Инструменты управления слайсером STE Slicer: профили, материалы, режим 3D/5D, сцена, слайсинг, экспорт.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._registry.mcp_tools()}
            elif method == "tools/call":
                name = params.get("name")
                if not name or not self._registry.has(name):
                    return _error(request_id, INVALID_PARAMS, "Unknown tool: %s" % name)
                outcome = self._registry.call(name, params.get("arguments") or {})
                result = {
                    "content": [{"type": "text", "text": json.dumps(outcome["result"], ensure_ascii=False)}],
                    "structuredContent": outcome["result"],
                    "isError": not outcome["ok"],
                }
            elif method.startswith("notifications/"):
                return None
            else:
                return _error(request_id, METHOD_NOT_FOUND, "Method not found: %s" % method)
        except Exception as e:
            return _error(request_id, INTERNAL_ERROR, str(e))

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
