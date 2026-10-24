"""Интеграционный тест плагина без слайсера: заглушки UM/PyQt5 + поддельная LLM.

Запуск: python tests/test_integration.py plugin"""
import base64
import enum
import json
import queue
import sys
import threading
import time
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PLUGIN_PARENT = sys.argv[1]  # .../plugin


def module(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


# ---------------- заглушки ----------------
class Signal:
    def __init__(self):
        self._slots = []

    def connect(self, f):
        self._slots.append(f)

    def disconnect(self, f):
        self._slots.remove(f)

    def emit(self, *a):
        for f in list(self._slots):
            f(*a)


class Prefs:
    def __init__(self):
        self.values = {}

    def addPreference(self, key, default):
        self.values.setdefault(key, default)

    def getValue(self, key):
        return self.values.get(key)

    def setValue(self, key, value):
        self.values[key] = value


class FakeApp:
    def __init__(self):
        self.queue = queue.Queue()
        self.prefs = Prefs()
        self.engineCreatedSignal = Signal()
        self.main = threading.Thread(target=self._loop, daemon=True, name="FakeQtMain")
        self.main.start()

    def _loop(self):
        while True:
            f, a, k = self.queue.get()
            f(*a, **k)

    def getMainThread(self):
        return self.main

    def callLater(self, f, *a, **k):
        self.queue.put((f, a, k))

    def getPreferences(self):
        return self.prefs

    def savePreferences(self):
        pass

    def getGlobalContainerStack(self):
        return None


APP = FakeApp()


class Application:
    @staticmethod
    def getInstance():
        return APP


class Logger:
    @staticmethod
    def log(level, msg, *args):
        print("[log %s] %s" % (level, msg % args if args else msg))


class BackendState(enum.IntEnum):
    NotStarted = 1
    Processing = 2
    Done = 3
    Error = 4
    Disabled = 5


class Stub:
    def __init__(self, *a, **k):
        pass


class ValidatorState(enum.Enum):
    Valid = "Valid"
    Unknown = "Unknown"


class Extension:
    def __init__(self):
        self.menu = {}

    def setMenuName(self, n):
        self.menu_name = n

    def addMenuItem(self, n, f):
        self.menu[n] = f


module("UM")
module("UM.Application", Application=Application)
module("UM.Logger", Logger=Logger)
module("UM.Extension", Extension=Extension)
module("UM.Message", Message=Stub)
module("UM.Backend")
module("UM.Backend.Backend", BackendState=BackendState)
module("UM.Math")
module("UM.Math.Quaternion", Quaternion=Stub)
module("UM.Math.Vector", Vector=Stub)
module("UM.Operations")
for op in ("RemoveSceneNodeOperation", "RotateOperation", "ScaleOperation", "TranslateOperation"):
    module("UM.Operations." + op, **{op: Stub})
module("UM.PluginRegistry", PluginRegistry=Stub)
module("UM.Scene")
module("UM.Scene.Iterator")
module("UM.Scene.Iterator.DepthFirstIterator", DepthFirstIterator=Stub)
module("UM.Scene.SceneNode", SceneNode=Stub)
module("UM.Settings")
module("UM.Settings.SettingDefinition", SettingDefinition=Stub)
module("UM.Settings.Validator", ValidatorState=ValidatorState)
module("UM.i18n", i18nCatalog=lambda name: None)
module("PyQt5")
module("PyQt5.QtCore", QObject=type("QObject", (), {"__init__": lambda self, parent=None: None}), QUrl=Stub,
       QBuffer=Stub, QByteArray=Stub, QIODevice=Stub, Qt=Stub)
module("PyQt5.QtGui", QDesktopServices=Stub, QImage=Stub)


# ---------------- поддельная LLM ----------------
REQUESTS = []
STREAMED = []


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json({"data": [{"id": "mock-gemma"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append(payload)
        last = payload["messages"][-1]
        if "новичок" in payload["messages"][0]["content"] and last["role"] == "user":
            message = {"role": "assistant", "content": "Где будет работать деталь?\nВарианты: дома | на улице | Реши сам"}
        elif last["role"] == "user" and payload.get("stream"):
            # Потоковый ответ: вызов инструмента приходит кусочками, как у настоящих серверов
            chunks = [
                {"choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "get_printer_state", "arguments": ""}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}}]},
            ]
            body = "".join("data: %s\n\n" % json.dumps(c) for c in chunks) + "data: [DONE]\n\n"
            STREAMED.append(True)
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        elif last["role"] == "user":
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_printer_state", "arguments": "{}"}}]}
        elif len(REQUESTS) == 2:
            # Второй шаг: модель пишет вызов текстом (как иногда делает Gemma)
            message = {"role": "assistant", "content": '```json\n{"name": "list_objects", "arguments": {}}\n```'}
        else:
            message = {"role": "assistant", "content": "**Режим:** 3D — тест пройден"}
        self._json({"choices": [{"message": message}]})


llm_server = HTTPServer(("127.0.0.1", 0), FakeLLM)
threading.Thread(target=llm_server.serve_forever, daemon=True).start()

# ---------------- плагин ----------------
sys.path.insert(0, PLUGIN_PARENT)
import os as _os, shutil as _shutil, tempfile as _tempfile  # noqa: E402
# Чаты прошлых прогонов мешают подсчёту событий — начинаем с чистой папки.
_shutil.rmtree(_os.path.join(_tempfile.gettempdir(), "aiassistant_chats"), ignore_errors=True)
import AIAssistant as plugin_package  # noqa: E402

registered = plugin_package.register(APP)
assistant = registered["extension"]
print("menu:", list(assistant.menu.keys()))
APP.engineCreatedSignal.emit()
base = assistant._server.base_url
token = assistant._token
print("server:", base)


def call(path, body=None, auth=True, host=None):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["X-Auth-Token"] = token
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw and r.headers.get("Content-Type", "").startswith("application/json") else raw[:60])
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


checks = []


def check(name, cond, extra=""):
    checks.append(cond)
    print("%s %s %s" % ("PASS" if cond else "FAIL", name, extra))


status, body = call("/", auth=False)
check("static index", status == 200)
status, _ = call("/api/config", auth=False)
check("no token -> 401", status == 401)
status, _ = call("/api/config", host="evil.example:80")
check("foreign Host -> 403", status == 403)
status, cfg = call("/api/config")
check("config with token", status == 200 and cfg["has_key"] is False and len(cfg["tools"]) == 28, str(len(cfg.get("tools", []))))
status, cfg = call("/api/config", {"base_url": "http://127.0.0.1:%d/v1" % llm_server.server_port, "api_key": "secret-key"})
check("set config, key hidden", status == 200 and cfg["has_key"] is True and "secret-key" not in json.dumps(cfg))
status, test = call("/api/test", {})
check("test connection", test.get("ok") and test["models"] == ["mock-gemma"], str(test))

status, init = call("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
check("mcp initialize", init["result"]["serverInfo"]["name"] == "ste-slicer-ai-assistant")
status, listed = call("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
names = [t["name"] for t in listed["result"]["tools"]]
check("mcp tools/list", len(names) == 28 and "slice" in names and all("inputSchema" in t for t in listed["result"]["tools"]))
status, called = call("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_printer_state", "arguments": {}}})
check("mcp tools/call error path", called["result"]["isError"] is True, called["result"]["content"][0]["text"])
status, unknown = call("/mcp", {"jsonrpc": "2.0", "id": 4, "method": "nope"})
check("mcp unknown method", unknown["error"]["code"] == -32601)
status, _ = call("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
check("mcp notification -> 202", status == 202)

stl = base64.b64encode(b"solid t\nendsolid t\n").decode()
status, sent = call("/api/message", {"text": "Шестерня, нарежь", "attachments": [{"name": "gear.stl", "data": "data:;base64," + stl}]})
check("message accepted", sent.get("ok") is True, str(sent))
status, busy = call("/api/message", {"text": "ещё"})
check("second message while busy rejected", busy.get("ok") is False)

events, after = [], 0
deadline = time.time() + 20
while time.time() < deadline:
    status, data = call("/api/events?after=%d" % after)
    for e in data["events"]:
        events.append(e)
        after = e["id"]
    if any(e["type"] == "run_end" for e in events):
        break
    time.sleep(0.2)
types_seen = [e["type"] for e in events]
print("events:", types_seen)
check("agent loop finished", "run_end" in types_seen and "assistant" in types_seen)
check("tool calls recorded", types_seen.count("tool_call") == 2 and types_seen.count("tool_result") == 2)
check("inline tool call parsed", any(e["type"] == "tool_call" and e["name"] == "list_objects" for e in events))
first_user = REQUESTS[0]["messages"][1]["content"]
check("model path passed to LLM", "gear.stl" in first_user and "load_model" in first_user)
check("auto model from /models", REQUESTS[0]["model"] == "mock-gemma")
check("tools sent to LLM", len(REQUESTS[0]["tools"]) == 28)
roles = [m["role"] for m in REQUESTS[-1]["messages"]]
print("roles in last request:", roles)
if "--serve" in sys.argv:
    open("ui_url.txt", "w").write("%s/#token=%s" % (base, token))
    print("SERVING", base)
    while True:
        time.sleep(3600)
status, sess = call("/api/sessions")
first = sess["current"]
check("sessions list has titled chat", any(x["id"] == first and x["title"].startswith("Шестерня") for x in sess["sessions"]), str(sess["sessions"]))
status, new = call("/api/sessions/new", {})
second = new["session"]
check("new session differs", second != first)
status, again = call("/api/sessions/new", {})
check("empty session reused", again["session"] == second)
status, sent = call("/api/message", {"text": "хочу держатель", "session": second, "mode": "novice"})
check("novice message accepted", sent.get("ok") is True, str(sent))
deadline = time.time() + 20
evs = []
while time.time() < deadline:
    status, data = call("/api/events?after=0&session=" + second)
    evs = data["events"]
    if any(e["type"] == "run_end" for e in evs):
        break
    time.sleep(0.2)
check("novice prompt used", "Режим общения: новичок" in REQUESTS[-1]["messages"][0]["content"])
check("options line returned", any(e["type"] == "assistant" and "Варианты:" in e["text"] for e in evs))
status, old = call("/api/events?after=0&session=" + first)
check("first chat events kept", any(e["type"] == "user" and e["text"].startswith("Шестерня") for e in old["events"]))
status, sel = call("/api/sessions/select", {"id": first})
check("select session", sel.get("ok") and sel["session"] == first)
import glob, os, tempfile
saved = glob.glob(os.path.join(tempfile.gettempdir(), "aiassistant_chats", second + ".json"))
check("chat saved to disk", len(saved) == 1)
status, deleted = call("/api/sessions/delete", {"id": second})
check("delete session", deleted.get("ok") and not glob.glob(os.path.join(tempfile.gettempdir(), "aiassistant_chats", second + ".json")))
status, reset = call("/api/reset", {})
check("reset opens new chat", reset.get("ok") is True and reset["session"] != first)

from AIAssistant.agent import tools as T, agent as A
found = T._resolve_model_path("data/models/1. Шестерня - 3D/Шестерня.stl")
check("relative model path resolved", found.endswith("Шестерня.stl") and os.path.isfile(found), found)
try:
    T._resolve_model_path("data/models/нет_такого.stl")
    check("missing model raises", False)
except Exception as e:
    check("missing model raises with hint", "скрепкой" in str(e))
g = A._LoopGuard()
same = {"function": {"name": "get_printer_state", "arguments": "{}"}}
warns = []
for i in range(6):
    w = g.check(same)
    warns.append(bool(w))
    g.record(same, '{"ok": 1}')
from AIAssistant.agent.sessions import EventLog
log = EventLog()
log.add("file_saved", kind="export_gcode", path="x")
check("event with kind field", log.all()[0]["kind"] == "export_gcode" and log.all()[0]["type"] == "file_saved")
check("streamed tool call assembled", bool(STREAMED))
check("loop guard on repeats", warns[:2] == [False, False] and warns[2] and g.tripped, str(warns))

# ---------------- жёсткие диапазоны, расход Q, проверка G-code ----------------
from AIAssistant.agent import knowledge as K


class RangeStack:
    def __init__(self, **props):
        self.props = props

    def getProperty(self, key, prop):
        return self.props.get(prop)


hard = RangeStack(minimum_value=0, maximum_value=100, minimum_value_warning=5, maximum_value_warning=90)
check("range: value inside", T._range_problem(hard, "infill_sparse_density", 40) is None)
check("range: above max rejected", T._range_problem(hard, "infill_sparse_density", 150) is not None)
check("range: recommended range enforced", T._range_problem(hard, "infill_sparse_density", 95) is not None)
K.MATERIAL_LIMITS["test_pa"] = {"material_print_temperature": (250, 280)}
temp = RangeStack(minimum_value=0, maximum_value=365)
check("range: material passport", T._range_problem(temp, "material_print_temperature", 300, "test_pa_x") is not None
      and T._range_problem(temp, "material_print_temperature", 260, "test_pa_x") is None)
del K.MATERIAL_LIMITS["test_pa"]
check("flow limits by material", K.max_volumetric_flow("enduse_tpu_d70") < K.max_volumetric_flow("fiberpart_pc_g20")
      <= K.max_volumetric_flow("enduse_petg"))

gdir = tempfile.mkdtemp()


def gcode(name, body):
    path = os.path.join(gdir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


bed = {"width": 300, "depth": 300, "height": 200, "center_is_zero": False, "printing_mode": "classic",
       "nozzle_temp": 260, "nozzle_max": 365, "bed_max": 120}
good = T._check_gcode(gcode("good.gcode", "M140 S80\nM109 S260\n;LAYER:0\nG1 X10 Y10 Z0.2 E1\nG1 X200 Y150 E2\nEND\nM104 S0\n"), bed)
check("gcode: good file valid", good["valid"] and not good["warnings"], str(good["issues"] + good["warnings"]))
outside = T._check_gcode(gcode("out.gcode", "M109 S260\nG1 X10 Y10 Z0.2 E1\nG1 X350 Y10 E2\nEND\nM104 S0\n"), bed)
check("gcode: off the bed", not outside["valid"] and any("X" in i for i in outside["issues"]))
hot = T._check_gcode(gcode("hot.gcode", "M109 S400\nG1 X10 Y10 Z0.2 E1\nEND\nM104 S0\n"), bed)
check("gcode: nozzle too hot", not hot["valid"])
five = dict(bed, printing_mode="cylindrical_full")
no_rotary = T._check_gcode(gcode("5d.gcode", "M109 S260\nG1 X0 Y5 Z3 E1\nEND\nM104 S0\n"), five)
check("gcode: 5D without A/C", not no_rotary["valid"])
rotary = T._check_gcode(gcode("5da.gcode", "M109 S260\nG0 X0 Y5 Z3 A90 C-30\nG1 C-29 E1\nEND\nM104 S0\n"), five)
check("gcode: 5D with A/C", rotary["valid"] and rotary["axes_range"]["A"] == [90.0, 90.0])
no_park = T._check_gcode(gcode("park.gcode", "M109 S260\nG1 X10 Y10 Z0.2 E1\n"), bed)
check("gcode: missing park warned", no_park["valid"] and len(no_park["warnings"]) == 2)
check("foreign script stripped", A._strip_foreign_script("стенка 核心区 под профиль") == "стенка  под профиль")

print("\n%d/%d checks passed" % (sum(checks), len(checks)))
sys.exit(0 if all(checks) else 1)
