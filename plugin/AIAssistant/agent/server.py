# Локальный HTTP-сервер: отдаёт SPA чата, REST API для неё и MCP-эндпоинт /mcp.
# Слушает только 127.0.0.1. Все /api и /mcp требуют токен сессии (генерируется при запуске слайсера),
# плюс проверка Host/Origin — защита от обращений со сторонних сайтов (CSRF, DNS rebinding).

import hmac
import json
import os
import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from UM.Logger import Logger

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
MAX_BODY_BYTES = 170 * 1024 * 1024


class _ThreadingServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # на Windows иначе два процесса могут занять один порт


class ChatServer:
    def __init__(self, web_dir, api, token):
        self._web_dir = web_dir
        self._api = api
        self._token = token
        self._httpd = None
        self._thread = None
        self.port = None

    @property
    def running(self):
        return self._httpd is not None

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.port

    def start(self, preferred_port):
        if self._httpd is not None:
            return
        handler = _make_handler(self)
        last_error = None
        for port in range(int(preferred_port), int(preferred_port) + 20):
            try:
                self._httpd = _ThreadingServer(("127.0.0.1", port), handler)
                self.port = port
                break
            except OSError as e:
                last_error = e
        if self._httpd is None:
            raise OSError("Не удалось занять порт для чата: %s" % last_error)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="AIAssistantHTTP", daemon=True)
        self._thread.start()
        Logger.log("i", "[AIAssistant] Chat server on %s", self.base_url)

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _make_handler(server):
    class Handler(BaseHTTPRequestHandler):
        server_version = "STE-AI-Assistant/0.1"

        def log_message(self, format, *args):  # не засоряем stderr слайсера
            pass

        # ------------------------------------------------------------ помощники

        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            allowed = {"127.0.0.1:%d" % server.port, "localhost:%d" % server.port}
            if self.headers.get("Host", "") not in allowed:
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in allowed}:
                return False
            return True

        def _token_ok(self):
            token = self.headers.get("X-Auth-Token", "")
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                token = token or auth[7:].strip()
            return bool(token) and hmac.compare_digest(token, server._token)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ValueError("Слишком большой запрос")
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            return raw

        def _guard(self):
            if not self._host_ok():
                self._send(403, {"error": "forbidden host/origin"})
                return False
            if not self._token_ok():
                self._send(401, {"error": "нужен токен сессии — откройте чат из меню слайсера"})
                return False
            return True

        # ------------------------------------------------------------ GET

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path in STATIC_FILES:
                if not self._host_ok():
                    return self._send(403, {"error": "forbidden host"})
                filename, content_type = STATIC_FILES[path]
                try:
                    with open(os.path.join(server._web_dir, filename), "rb") as stream:
                        return self._send(200, stream.read(), content_type)
                except OSError:
                    return self._send(404, {"error": "not found"})
            if path == "/mcp":
                return self._send(405, {"error": "use POST for MCP JSON-RPC"})
            if not path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._guard():
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if path == "/api/events":
                    after = int((query.get("after") or ["0"])[0])
                    session = (query.get("session") or [""])[0]
                    return self._send(200, server._api.api_events(after, session))
                if path == "/api/sessions":
                    return self._send(200, server._api.api_sessions())
                if path == "/api/config":
                    return self._send(200, server._api.api_get_config())
                if path == "/api/state":
                    return self._send(200, server._api.api_state())
            except Exception as e:
                Logger.log("w", "[AIAssistant] GET %s failed: %s", path, e)
                return self._send(500, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        # ------------------------------------------------------------ POST

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if not self._guard():
                return
            try:
                raw = self._read_json()
                if path == "/mcp":
                    response = server._api.api_mcp(raw)
                    if response is None:
                        self.send_response(202)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    return self._send(200, response)
                body = json.loads(raw) if raw else {}
                if path == "/api/message":
                    return self._send(200, server._api.api_message(
                        body.get("text", ""), body.get("attachments") or [], body.get("session"), body.get("mode")))
                if path == "/api/sessions/new":
                    return self._send(200, server._api.api_session_new())
                if path == "/api/sessions/select":
                    return self._send(200, server._api.api_session_select(str(body.get("id", ""))))
                if path == "/api/sessions/delete":
                    return self._send(200, server._api.api_session_delete(str(body.get("id", ""))))
                if path == "/api/config":
                    return self._send(200, server._api.api_set_config(body))
                if path == "/api/test":
                    return self._send(200, server._api.api_test())
                if path == "/api/reset":
                    return self._send(200, server._api.api_reset())
                if path == "/api/stop":
                    return self._send(200, server._api.api_stop())
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                Logger.log("w", "[AIAssistant] POST %s failed: %s", path, e)
                return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
            return self._send(404, {"error": "not found"})

    return Handler
