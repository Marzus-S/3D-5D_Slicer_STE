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
    checks.append(bool(cond))
    print("%s %s %s" % ("PASS" if cond else "FAIL", name, extra))


status, body = call("/", auth=False)
check("static index", status == 200)
status, _ = call("/api/config", auth=False)
check("no token -> 401", status == 401)
status, _ = call("/api/config", host="evil.example:80")
check("foreign Host -> 403", status == 403)
status, cfg = call("/api/config")
check("config with token", status == 200 and cfg["has_key"] is False and len(cfg["tools"]) == 35, str(len(cfg.get("tools", []))))
status, cfg = call("/api/config", {"base_url": "http://127.0.0.1:%d/v1" % llm_server.server_port, "api_key": "secret-key"})
check("set config, key hidden", status == 200 and cfg["has_key"] is True and "secret-key" not in json.dumps(cfg))
status, test = call("/api/test", {})
check("test connection", test.get("ok") and test["models"] == ["mock-gemma"], str(test))

status, init = call("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
check("mcp initialize", init["result"]["serverInfo"]["name"] == "ste-slicer-ai-assistant")
status, listed = call("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
names = [t["name"] for t in listed["result"]["tools"]]
check("mcp tools/list", len(names) == 35 and "slice" in names and all("inputSchema" in t for t in listed["result"]["tools"]))
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
check("tools sent to LLM", len(REQUESTS[0]["tools"]) == 35)
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
from AIAssistant.agent import learning as L


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
check("task text without attachment list", A._first_user_text(
    [{"role": "user", "content": "Колесо конвейера\n\n[Приложены 3D-модели — загрузи их через load_model]\n- a.stl: C:/a.stl"}])
      == "Колесо конвейера")

# ---------------- предпечатная проверка и 5D-советник на синтетических сетках ----------------
import hashlib
import math
import numpy
from AIAssistant.agent import advisor as V, geometry as G, passport as P


def oriented(triangles, outward):
    """Порядок вершин так, чтобы нормаль смотрела в сторону outward (ось сцены Y — вверх)."""
    triangles = numpy.array(triangles, dtype=float)
    normals = numpy.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    flip = (normals * numpy.array(outward, dtype=float)).sum(axis=1) < 0
    triangles[flip] = triangles[flip][:, [0, 2, 1]]
    return triangles


def box(sx, sy, sz, origin=(0, 0, 0)):
    o = numpy.array(origin, dtype=float)
    c = [o + numpy.array([x * sx, y * sy, z * sz]) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    faces = [((0, 1, 3, 2), (-1, 0, 0)), ((4, 6, 7, 5), (1, 0, 0)), ((0, 4, 5, 1), (0, -1, 0)),
             ((2, 3, 7, 6), (0, 1, 0)), ((0, 2, 6, 4), (0, 0, -1)), ((1, 5, 7, 3), (0, 0, 1))]
    tris, out = [], []
    for (a, b, d, e), normal in faces:
        tris += [[c[a], c[b], c[d]], [c[a], c[d], c[e]]]
        out += [normal, normal]
    return oriented(tris, out)


def tube(outer, inner, length, segments=96):
    """Труба (inner > 0) или цилиндр (inner = 0) вдоль оси Y сцены."""
    tris, out = [], []
    for i in range(segments):
        a0, a1 = 2 * math.pi * i / segments, 2 * math.pi * (i + 1) / segments
        ring = lambda r, a, y: [r * math.cos(a), y, r * math.sin(a)]
        mid = (a0 + a1) / 2
        radial = [math.cos(mid), 0, math.sin(mid)]
        for r, sign in ((outer, 1),) + (((inner, -1),) if inner else ()):
            p = [ring(r, a0, 0), ring(r, a1, 0), ring(r, a1, length), ring(r, a0, length)]
            tris += [[p[0], p[1], p[2]], [p[0], p[2], p[3]]]
            out += [[sign * v for v in radial]] * 2
        for y, normal in ((0, [0, -1, 0]), (length, [0, 1, 0])):
            if inner:
                p = [ring(inner, a0, y), ring(outer, a0, y), ring(outer, a1, y), ring(inner, a1, y)]
                tris += [[p[0], p[1], p[2]], [p[0], p[2], p[3]]]
                out += [normal, normal]
            else:
                tris.append([[0, y, 0], ring(outer, a0, y), ring(outer, a1, y)])
                out.append(normal)
    return oriented(tris, out)


def inspect(triangles, load=None, flexible=False):
    analysis = G.analyze(triangles.reshape(-1, 3))
    health = G.mesh_health(analysis["triangles"])
    thickness = G.wall_thickness(analysis["triangles"], analysis["normals"], analysis["areas"])
    levels = {c["id"]: c for c in V.preflight(analysis, health, thickness, density=1.2)["checks"]}
    return analysis, health, levels, V.five_axis(analysis, load, flexible, density=1.2)


cube, cube_health, cube_checks, _ = inspect(box(20, 20, 20))
check("preflight: clean cube", cube_health["watertight"] and cube_health["bodies"] == 1
      and all(c["level"] == "ok" for c in cube_checks.values()), str(cube_checks))
_, _, inch_checks, _ = inspect(box(2, 1, 1.5))
check("preflight: inches suspected with ×25.4 fix", inch_checks["units"]["level"] == "fail"
      and inch_checks["units"]["fix"]["args"]["percent"] == 2540, str(inch_checks["units"]))
_, open_health, open_checks, _ = inspect(box(20, 20, 20)[:-2])
check("preflight: hole in mesh", open_health["open_edges"] > 0 and open_checks["mesh"]["level"] != "ok")
_, two_health, two_checks, _ = inspect(numpy.concatenate([box(10, 10, 10), box(10, 10, 10, (30, 0, 0))]))
check("preflight: two bodies", two_health["bodies"] == 2 and two_checks["bodies"]["level"] == "warn")
_, _, thin_checks, _ = inspect(box(30, 0.5, 30))
check("preflight: thin wall", thin_checks["walls"]["level"] == "warn" and "0.5" in thin_checks["walls"]["text"],
      thin_checks["walls"]["text"])
inverted, _, inverted_checks, _ = inspect(box(20, 20, 20)[:, [0, 2, 1]])
check("preflight: inverted normals fixed and reported", inverted["inverted"] and "normals" in inverted_checks)

solid = G.analyze(tube(20, 0, 10, segments=256).reshape(-1, 3))
outside = G.volume_outside_radius(solid["triangles"], solid["normals"], solid["areas"],
                                  numpy.array([0.0, 1.0, 0.0]), numpy.zeros(3), 10.0)
check("volume outside base radius", abs(outside - math.pi * (400 - 100) * 10) / (math.pi * 300 * 10) < 0.01, "%.1f" % outside)

roller, _, roller_checks, roller_5d = inspect(tube(20, 10, 40), load="torsion")
check("5D: shaft sleeve under torsion", roller_5d["verdict"] == "5d" and roller_5d["mode"] == "cylindrical_full"
      and 19.8 <= roller_5d["base_diameter_mm"] <= 20.0 and roller_5d["evidence"], str(roller_5d["score"]))
check("preflight: bore tolerance IT12", roller_checks["hole"]["level"] == "info" and "0.21" in roller_checks["hole"]["text"])
_, _, _, disc_5d = inspect(tube(40, 5, 6), load="bending")
check("5D: thin disc under bending -> 3D", disc_5d["verdict"] == "3d", str(disc_5d["factors"]))
_, _, _, soft_5d = inspect(tube(20, 10, 40), flexible=True)
check("5D: flexible material blocked", soft_5d["verdict"] == "3d" and soft_5d["blockers"])
_, _, _, plate_5d = inspect(box(40, 5, 25))
check("5D: no axis blocked", plate_5d["verdict"] == "3d" and any("оси" in b for b in plate_5d["blockers"]))
check("5D: base without bore 6–10 mm", V.base_diameter({"through_bore": False, "bore_diameter": 0,
                                                         "outer_diameter": 100})[0] == 10.0)

# ---------------- паспорт рецепта ----------------
pdir = tempfile.mkdtemp()
model_file = gcode("model.stl", "solid t\nendsolid t\n")
state = {"printer": "Stereotech Hybrid 530 V5.2.4", "printer_definition": "hte530", "slicer_version": "7.10.12",
         "printing_mode": "classic", "printing_mode_label": "3D", "quality": "Normal", "quality_type": "normal",
         "material": {"id": "enduse_pa6", "name": "Enduse PA6", "brand": "Stereotech", "density": 1.14},
         "nozzle": "0.4", "objects": [{"name": "Колесо", "size_mm": {"x": 40, "y": 40, "z": 40}, "scale_percent": 100}],
         "settings": [{"key": "wall_line_count", "label": "Wall Line Count", "value": 5, "unit": ""},
                      {"key": "printing_mode", "label": "Printing Mode", "value": "classic", "unit": ""}],
         "result": {"print_time": "2 ч 05 мин", "total_weight_g": 48.3}}
record = P.build(state, model_path=model_file, analysis={"preflight": {"checks": list(roller_checks.values())},
                                                         "five_axis": roller_5d},
                 plan={"orientation": "min_supports"},
                 rationale={"mode": "так быстрее", "wall_line_count": "нагрузка на кручение"},
                 gcode_path=good and gcode("good2.gcode", "G1 X1\n"), gcode_check=good, project_path=None,
                 context={"llm_model": "mock-gemma", "task": "Колесо конвейера, PA6"})
paths = P.write(pdir, record)
markdown = open(paths["markdown"], encoding="utf-8").read()
saved = json.load(open(paths["json"], encoding="utf-8"))
check("passport: files written", saved["format"] == "ste-ai-recipe/1" and saved["status"].startswith("черновик"))
check("passport: hashes", saved["part"]["model"]["sha256"] == hashlib.sha256(open(model_file, "rb").read()).hexdigest())
check("passport: rationale in settings", saved["settings"][0]["why"] == "нагрузка на кручение"
      and saved["rationale"] == {"mode": "так быстрее"})
check("passport: mode not duplicated in settings", [s["key"] for s in saved["settings"]] == ["wall_line_count"])
check("passport: mode differs from advice noted", any("5D-советника" in n for n in saved["notes"]), str(saved["notes"]))
check("passport: markdown sections", all(s in markdown for s in ("## Решения", "## 5D-советник", "## Изменённые настройки",
                                                                 "## Заполняет технолог", "mock-gemma", "Колесо конвейера")))

# ---------------- режимы чата, быстрые команды, квиз, рецепты, разбор настроек ----------------
def run_until_end(session):
    deadline = time.time() + 20
    while time.time() < deadline:
        status, data = call("/api/events?after=0&session=" + session)
        if not data["busy"] and any(e["type"] == "run_end" for e in data["events"]):
            return data["events"]
        time.sleep(0.2)
    return data["events"]


g = A._LoopGuard()
broken = {"function": {"name": "slice", "arguments": "{}"}}
for i in range(3):
    g.record(dict(broken, function={"name": "slice", "arguments": '{"n": %d}' % i}), '{"error": "нет модели"}')
check("loop guard counts failures", bool(g.check({"function": {"name": "slice", "arguments": '{"n": 9}'}})))

status, new = call("/api/sessions/new", {})
modes_chat = new["session"]
before = len(REQUESTS)
call("/api/message", {"text": "научи меня", "session": modes_chat, "mode": "learning"})
run_until_end(modes_chat)
learning_request = REQUESTS[before]
learning_tools = [t["function"]["name"] for t in learning_request["tools"]]
check("learning prompt used", "Режим общения: обучение" in learning_request["messages"][0]["content"])
check("learning mode tools", "quiz" in learning_tools and "review_my_settings" in learning_tools
      and "apply_recipe" not in learning_tools, str(len(learning_tools)))
before = len(REQUESTS)
call("/api/message", {"text": "шестерня", "session": modes_chat, "mode": "pro"})
run_until_end(modes_chat)
pro_tools = [t["function"]["name"] for t in REQUESTS[before]["tools"]]
check("pro mode tools", "apply_recipe" in pro_tools and "quiz" not in pro_tools
      and "Режим общения: профи" in REQUESTS[before]["messages"][0]["content"])

status, new = call("/api/sessions/new", {})
cmd_chat = new["session"]
before = len(REQUESTS)
call("/api/message", {"text": "/help", "session": cmd_chat, "mode": "pro"})
evs = run_until_end(cmd_chat)
check("command answered without LLM", len(REQUESTS) == before and any(
    e["type"] == "assistant" and "/infill" in e["text"] and e.get("command_seconds") is not None for e in evs))
call("/api/message", {"text": "/infill 40", "session": cmd_chat, "mode": "pro"})
evs = run_until_end(cmd_chat)
check("command calls tool directly", len(REQUESTS) == before and any(
    e["type"] == "tool_call" and e["name"] == "set_settings" and e["args"] == {"settings": {"infill_sparse_density": 40}}
    for e in evs))
call("/api/message", {"text": "/nope", "session": cmd_chat})
evs = run_until_end(cmd_chat)
check("unknown command explained", any(e["type"] == "assistant" and "/help" in e["text"] for e in evs[-3:]))
call("/api/message", {"text": "/analyze кручение", "session": cmd_chat})
evs = run_until_end(cmd_chat)
check("analyze command", len(REQUESTS) == before and any(
    e["type"] == "tool_call" and e["name"] == "analyze_model" and e["args"] == {"load": "torsion"} for e in evs)
)
from AIAssistant.agent import commands as C
fake = {"preflight": {"summary": "ошибки — 0"}, "five_axis": {
    "verdict": "5d", "score": 4, "base_diameter_mm": 30, "blockers": [], "decide": "Решает технолог",
    "factors": [{"points": 1, "why": "мелкое"}, {"points": 3, "why": "кручение вдоль слоёв"}]}}
text = C.execute("/analyze кручение гибкая", lambda tool, args: (True, fake))
check("analyze formatted", "печатать в 5D" in text and "балл 4" in text and "⌀30" in text
      and text.index("+3") < text.index("+1"), text)
status, listed_cmds = call("/api/commands")
check("commands catalog", any(c["name"] == "recipe" for c in listed_cmds["commands"]) and "level" in listed_cmds["progress"])

call("/api/message", {"text": "/quiz материал", "session": cmd_chat})
evs = run_until_end(cmd_chat)
quizzes = [e for e in evs if e["type"] == "quiz"]
check("quiz shown as cards", len(quizzes) == 2 and all(len(q["options"]) >= 2 for q in quizzes), str(len(quizzes)))
question = next(q for q in L.QUESTIONS if q[0] == quizzes[0]["qid"]) if quizzes else None
status, answer = call("/api/quiz", {"session": cmd_chat, "event": quizzes[0]["id"], "choice": question[3][0]})
check("quiz answer checked by code", answer.get("ok") and answer["correct"] is True and answer["score"]["solved"] >= 1, str(answer))
status, again = call("/api/quiz", {"session": cmd_chat, "event": quizzes[0]["id"], "choice": question[3][1]})
check("quiz answered once", again.get("ok") is False)
status, wrong = call("/api/quiz", {"session": cmd_chat, "event": quizzes[1]["id"], "choice": "не тот ответ"})
check("quiz wrong answer", bool(wrong.get("ok") and wrong["correct"] is False and wrong["why"]))
status, data = call("/api/events?after=0&session=" + cmd_chat)
check("quiz answers in chat", sum(1 for e in data["events"] if e["type"] == "quiz_answer") == 2)
done = [e for e in data["events"] if e["type"] == "quiz_done"]
check("quiz summary after last answer", len(done) == 1 and done[0]["right"] == 1 and done[0]["total"] == 2
      and done[0]["topic"] == "material", str(done))

reviewed = L.review({"mode": "cylindrical_full", "material_id": "enduse_tpu_d70",
                     "values": {"infill_sparse_density": {"value": 15}, "wall_line_count": {"value": 5},
                                "top_layers": {"value": 6}, "bottom_layers": {"value": 6},
                                "adhesion_type": {"value": "brim"}, "speed_print": {"value": 60}},
                     "user_changes": {"infill_sparse_density": 15}}, "loaded")
keys = [f["key"] for f in reviewed["findings"]]
check("review finds mistakes", keys == ["infill_sparse_density", "adhesion_type", "printing_mode", "speed_print", "material"]
      and "wall_line_count" in reviewed["ok"], str(keys))
check("review labels material", reviewed["findings"][-1]["label"] == "Материал")
fresh_quiz = L.LearningTools(os.path.join(tempfile.mkdtemp(), "p.json"))
adhesion = [q for q in L.QUESTIONS if q[1] == "adhesion"]
for q in adhesion[:-1]:
    fresh_quiz.check(q[0], q[3][0])
batch = fresh_quiz.quiz("adhesion", 2)["questions"]
check("quiz fills batch with solved", len(batch) == 2 and batch[0]["id"] == adhesion[-1][0], str([b["id"] for b in batch]))
check("review default purpose", "note" in L.review({"values": {}}, None))

from AIAssistant.agent import materials as M
pa = M.info("enduse_pa6")
check("material as printed", pa["family"] == "pa" and pa["drying"]["temp_c"] == [70, 80]
      and pa["z_ratio"]["value"] == 0.9 and all(p["source"] == "printed" for p in pa["properties"]), str(pa["z_ratio"]))
check("material families", [M.family_of(m) for m in ("enduse_tpu_d70", "enduse_petg", "fiberpart_pc_g20", "ABS", "x")]
      == ["flex", "petg", "pc", "abs", None])
tpu = M.info("enduse_tpu_d70")
check("no z data falls back to range", tpu["z_ratio"]["source"] == "ornl" and "ornl" in [s["key"] for s in tpu["sources"]])
check("filled needs second printblock", M.info("fiberpart_pp_g30")["printblock"]["value"] == 2
      and "printblock" not in pa)
text = C.execute("/matinfo pa6", lambda tool, args: (True, M.info(args["material"])))
check("matinfo command cites sources", "50 МПа [1]" in text and "Сушка: 70–80 °C, 8–12 ч" in text
      and "qidi3d.com" in text, text)
check("pla not hygroscopic", M.drying_hint("enduse_pla") is None and "сушите" in M.drying_hint("enduse_pa6"))

from AIAssistant.agent.recipes import RecipeStore
store = RecipeStore(os.path.join(tempfile.mkdtemp(), "r.json"))
store.put({"name": "Шестерня PA6", "printing_mode": "classic", "material_id": "enduse_pa6", "settings": {"wall_line_count": 5}})
store.put({"name": "Ролик 5D", "printing_mode": "cylindrical_full", "material_id": "enduse_tpu_d70"})
check("recipe by part of name", store.get("шестерня")["material_id"] == "enduse_pa6")
try:
    store.get("нет такого")
    check("unknown recipe raises", False)
except ValueError as e:
    check("unknown recipe lists names", "Ролик 5D" in str(e))

print("\n%d/%d checks passed" % (sum(checks), len(checks)))
sys.exit(0 if all(checks) else 1)
