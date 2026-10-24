# Инструменты над API STE Slicer. Публичные методы можно вызывать из любого потока:
# всё, что трогает сцену и настройки, уходит в главный поток через run_on_main.
# Методы с префиксом _ предполагают, что уже выполняются в главном потоке.

import json
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict

from PyQt5.QtCore import QUrl

from UM.Application import Application
from UM.Backend.Backend import BackendState
from UM.Logger import Logger
from UM.Math.Quaternion import Quaternion
from UM.Math.Vector import Vector
from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
from UM.Operations.RotateOperation import RotateOperation
from UM.Operations.ScaleOperation import ScaleOperation
from UM.Operations.TranslateOperation import TranslateOperation
from UM.PluginRegistry import PluginRegistry
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
from UM.Scene.SceneNode import SceneNode
from UM.Settings.SettingDefinition import SettingDefinition
from UM.Settings.Validator import ValidatorState

from . import advisor, geometry, knowledge, learning, passport
from .mainthread import is_main_thread, run_on_main
from .registry import ToolError

try:
    from UM.i18n import i18nCatalog
    _settings_catalog = i18nCatalog("fdmprinter.def.json")
except Exception:
    _settings_catalog = None

PRINTING_MODES = OrderedDict([
    ("classic", "3D"),
    ("cylindrical", "5D Spiral"),
    ("cylindrical_full", "5D"),
])

KEY_SETTINGS = [
    "printing_mode", "layer_height", "wall_line_count", "top_layers", "bottom_layers",
    "infill_sparse_density", "infill_pattern", "support_enable", "support_type", "adhesion_type",
    "speed_print", "material_print_temperature", "material_bed_temperature", "cool_fan_speed",
    "cylindrical_mode_base_diameter",
]

# Что смотрит разбор настроек ученика (review_my_settings) помимо его собственных изменений.
REVIEW_KEYS = [
    "layer_height", "wall_line_count", "top_layers", "bottom_layers", "infill_sparse_density", "infill_pattern",
    "support_enable", "adhesion_type", "speed_print",
]
ORIENTATIONS = ["min_supports", "max_contact", "axis_vertical", "none"]

# Оси принтера → оси сцены Uranium (в сцене Y смотрит вверх).
PRINTER_AXES = {"x": Vector(1, 0, 0), "y": Vector(0, 0, -1), "z": Vector(0, 1, 0)}

STATE_NAMES = {
    BackendState.NotStarted: "not_started",
    BackendState.Processing: "processing",
    BackendState.Done: "done",
    BackendState.Error: "error",
    BackendState.Disabled: "disabled",
}


def _app():
    return Application.getInstance()


def _global_stack():
    stack = _app().getGlobalContainerStack()
    if stack is None:
        raise ToolError("В слайсере не выбран принтер")
    return stack


def _extruders(global_stack):
    return [global_stack.extruders[k] for k in sorted(global_stack.extruders.keys(), key=int)]


def _extruder(global_stack, index):
    key = str(int(index or 0))
    if key not in global_stack.extruders:
        raise ToolError("Нет экструдера %s (есть: %s)" % (key, ", ".join(sorted(global_stack.extruders.keys()))))
    return global_stack.extruders[key]


def _definition(global_stack, key):
    definitions = global_stack.definition.findDefinitions(key=key)
    return definitions[0] if definitions else None


def _label(definition):
    if _settings_catalog is not None:
        try:
            translated = _settings_catalog.i18nc("%s label" % definition.key, definition.label)
            if translated:
                return translated
        except Exception:
            pass
    return definition.label


def _description(definition):
    """Описание настройки из слайсера, по возможности на русском (из каталога переводов)."""
    text = definition.description or ""
    if _settings_catalog is not None:
        try:
            text = _settings_catalog.i18nc("%s description" % definition.key, text) or text
        except Exception:
            pass
    return " ".join(text.split())


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _stack_for(global_stack, definition, extruder=None):
    if definition.settable_per_extruder:
        return _extruder(global_stack, extruder or 0)
    return global_stack


def _validation(stack, definition):
    try:
        validator_type = SettingDefinition.getValidatorForType(definition.type)
        if validator_type is None:
            return None
        state = validator_type(definition.key)(stack)
        if state in (None, ValidatorState.Valid, ValidatorState.Unknown):
            return None
        return state.value
    except Exception:
        return None


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _range_problem(stack, key, value, material_id=None):
    """Границы для агента: min/max настройки, её рекомендуемый диапазон (у части настроек слайсер задаёт
    только его — например, заполнение до 100 %) и паспорт материала из knowledge. Возвращает текст
    ошибки или None; значение с ошибкой не применяется. Человек вручную может выйти за рекомендуемый
    диапазон, агент — нет."""
    number = _number(value)
    if number is None or isinstance(value, bool):
        return None
    low, high = None, None
    for prop_low, prop_high in (("minimum_value", "maximum_value"), ("minimum_value_warning", "maximum_value_warning")):
        bound = _number(stack.getProperty(key, prop_low))
        if bound is not None:
            low = bound if low is None else max(low, bound)
        bound = _number(stack.getProperty(key, prop_high))
        if bound is not None:
            high = bound if high is None else min(high, bound)
    passport = knowledge.material_limits(material_id).get(key)
    if passport:
        low = passport[0] if low is None else max(low, passport[0])
        high = passport[1] if high is None else min(high, passport[1])
    if (low is not None and number < low) or (high is not None and number > high):
        return "значение %s вне допустимого диапазона [%s; %s]" % (
            _jsonable(value), "—" if low is None else _jsonable(low), "—" if high is None else _jsonable(high))
    return None


def _material_id(stack):
    material = getattr(stack, "material", None)
    if material is None or material.getId() == "empty_material":
        material = _extruder(_global_stack(), 0).material
    return material.getMetaDataEntry("base_file", material.getId())


def _flow_check(global_stack):
    """Объёмный расход Q = v × h × w (мм³/с) против ориентира хотэнда для материала из knowledge."""
    try:
        stack = _extruder(global_stack, 0)
        speed = max(_number(stack.getProperty("speed_print", "value")) or 0,
                    _number(stack.getProperty("speed_infill", "value")) or 0)
        height_key = "layer_height"
        if global_stack.getProperty("printing_mode", "value") != "classic" and \
                global_stack.getProperty("cylindrical_layer_height", "value") is not None:
            height_key = "cylindrical_layer_height"
        height = _number(global_stack.getProperty(height_key, "value"))
        width = _number(stack.getProperty("line_width", "value"))
        if not (speed and height and width):
            return None
        flow = speed * height * width
        limit = knowledge.max_volumetric_flow(_material_id(stack))
        result = {"volumetric_flow_mm3_s": round(flow, 1), "limit_mm3_s": limit,
                  "formula": "Q = %g мм/с × %g мм × %g мм" % (speed, height, width)}
        if flow > limit:
            result["warning"] = ("Q выше ориентира для материала: недоэкструзия и слабое спекание слоёв. "
                                 "Снизьте скорость до ≈ %d мм/с" % int(limit / (height * width)))
        return result
    except Exception as e:
        Logger.log("w", "[AIAssistant] flow check failed: %s", e)
        return None


def _convert(definition, raw):
    kind = definition.type
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes", "on", "да"):
            return True
        if text in ("false", "0", "no", "off", "нет"):
            return False
        raise ValueError("ожидается true/false")
    if kind == "int":
        return int(round(float(raw)))
    if kind == "float":
        return float(raw)
    if kind in ("extruder", "optional_extruder"):
        return str(int(float(raw)))
    if kind == "enum":
        options = definition.options or {}
        if raw in options:
            return raw
        text = str(raw).strip().lower()
        for key, label in options.items():
            if text in (str(key).lower(), str(label).lower()):
                return key
        raise ValueError("допустимые значения: %s" % ", ".join(options.keys()))
    if isinstance(raw, str):
        return raw
    return json.dumps(raw)


def _safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip().strip(".")
    return name or "model"


def _format_duration(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return "%d ч %02d мин" % (hours, minutes)
    return "%d мин" % minutes


def _scene_nodes():
    root = _app().getController().getScene().getRoot()
    nodes = []
    for node in DepthFirstIterator(root):
        if node is root or not isinstance(node, SceneNode):
            continue
        if not (node.callDecoration("isSliceable") or node.callDecoration("isGroup")):
            continue
        parent = node.getParent()
        if parent is not None and parent.callDecoration("isGroup"):
            continue
        if not node.getMeshData() and not node.callDecoration("isGroup"):
            continue
        nodes.append(node)
    return nodes


def _find_node(ref=None):
    nodes = _scene_nodes()
    if not nodes:
        raise ToolError("Сцена пуста — сначала загрузите модель (load_model)")
    names = ["%d: %s" % (i, n.getName()) for i, n in enumerate(nodes)]
    if ref is None or ref == "":
        if len(nodes) == 1:
            return nodes[0]
        raise ToolError("На столе несколько объектов, укажите object: " + "; ".join(names))
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        index = int(ref)
        if 0 <= index < len(nodes):
            return nodes[index]
        raise ToolError("Нет объекта с номером %s. Объекты: %s" % (ref, "; ".join(names)))
    text = str(ref).strip().lower()
    exact = [n for n in nodes if n.getName().lower() == text]
    if exact:
        return exact[0]
    partial = [n for n in nodes if text in n.getName().lower()]
    if len(partial) == 1:
        return partial[0]
    raise ToolError("Объект «%s» не найден однозначно. Объекты: %s" % (ref, "; ".join(names)))


def _node_info(node, index=None):
    box = node.getBoundingBox()
    info = {"name": node.getName()}
    if index is not None:
        info["index"] = index
    if box is not None:
        info["size_mm"] = {"x": round(box.width, 2), "y": round(box.depth, 2), "z": round(box.height, 2)}
        info["center_mm"] = {"x": round(box.center.x, 2), "y": round(-box.center.z, 2)}
        info["bottom_z_mm"] = round(box.bottom, 2)
    scale = node.getScale()
    info["scale_percent"] = round(scale.x * 100.0, 1)
    outside = getattr(node, "isOutsideBuildArea", None)
    if callable(outside):
        info["outside_build_area"] = bool(outside())
    return info


def _drop_to_plate(node):
    box = node.getBoundingBox()
    if box is not None and abs(box.bottom) > 1e-3:
        TranslateOperation(node, Vector(0, -box.bottom, 0)).push()


def _rotate(node, quaternion):
    RotateOperation(node, quaternion, rotate_around_point=node.getPosition()).push()
    _drop_to_plate(node)


def _move_center_to(node, x, y):
    box = node.getBoundingBox()
    delta = Vector(float(x) - box.center.x, 0, -float(y) - box.center.z)
    if delta.length() > 1e-4:
        TranslateOperation(node, delta).push()


def _world_mesh(node):
    if node.callDecoration("isGroup"):
        raise ToolError("Анализ групп не поддерживается — разгруппируйте объекты")
    mesh = node.getMeshData()
    if mesh is None:
        raise ToolError("У объекта нет сетки")
    transformed = mesh.getTransformed(node.getWorldTransformation())
    vertices = transformed.getVertices().copy()
    indices = transformed.getIndices().copy() if transformed.hasIndices() else None
    return vertices, indices


def _advisor_context(node):
    """Что советнику нужно из слайсера: масштаб модели, ширина линии, плотность материала, скорость и перекрытие 5D."""
    global_stack = _global_stack()
    extruder = _extruder(global_stack, 0)
    properties = extruder.material.getMetaDataEntry("properties", {}) or {}
    return {
        "scale_percent": node.getScale().x * 100.0,
        "line_width": _number(extruder.getProperty("line_width", "value")) or 0.4,
        "density": _number(properties.get("density")),
        "speed": _number(extruder.getProperty("speed_wall_0_cylindrical", "value")) or 30.0,
        "overlap": _number(global_stack.getProperty("cylindrical_mode_overlap", "value")) or 0.0,
    }


class _BackendListener:
    """Держит сильную ссылку на обработчики: сигналы Uranium хранят слабые ссылки."""

    def __init__(self, watcher, backend_id):
        self._watcher = watcher
        self._backend_id = backend_id

    def on_state(self, state):
        self._watcher.on_state(self._backend_id, state)

    def on_progress(self, amount):
        self._watcher.on_progress(self._backend_id, amount)


class _SettingsListener:
    def __init__(self, watcher):
        self._watcher = watcher

    def on_property(self, key, property_name):
        if property_name == "value":
            self._watcher.last_setting_change = time.time()


class SliceWatcher:
    def __init__(self):
        self._lock = threading.Lock()
        self._listeners = {}
        self._settings_listener = _SettingsListener(self)
        self._watched_stacks = set()
        self.active_backend = None
        self.state = None
        self.progress = 0.0
        self.changes = 0
        self.progress_callback = None
        self.last_setting_change = 0.0
        self.last_activity = 0.0

    def ensure_connected(self):
        """Подключает слушатели; True — если слежение за настройками только что началось."""
        return run_on_main(self._connect)

    def _connect(self):
        fresh = False
        for backend_id, backend in _app().getBackendManager().getBackends().items():
            if backend_id in self._listeners:
                continue
            listener = _BackendListener(self, backend_id)
            backend.backendStateChange.connect(listener.on_state)
            backend.processingProgress.connect(listener.on_progress)
            self._listeners[backend_id] = listener
        global_stack = _app().getGlobalContainerStack()
        if global_stack is not None:
            for stack in [global_stack] + _extruders(global_stack):
                if stack.getId() not in self._watched_stacks:
                    stack.propertyChanged.connect(self._settings_listener.on_property)
                    self._watched_stacks.add(stack.getId())
                    fresh = True
        return fresh

    def wait_settled(self, quiet=2.5, limit=30):
        """Ждём, пока настройки перестанут меняться и проверка ошибок слайсера закончится. Если менять
        настройки во время нарезки, слайсер стирает G-code посреди работы движка и падает (KeyError)."""
        started = time.time()
        while time.time() - started < limit:
            checker_busy = run_on_main(lambda: bool(getattr(_app().getMachineErrorChecker(), "needToWaitForResult", False)))
            if not checker_busy and time.time() - self.last_setting_change >= quiet:
                return True
            time.sleep(0.3)
        return False

    def reset(self, backend_id):
        with self._lock:
            self.active_backend = backend_id
            self.state = None
            self.progress = 0.0
            self.changes = 0

    def on_state(self, backend_id, state):
        with self._lock:
            if backend_id != self.active_backend:
                return
            self.state = state
            self.changes += 1
            self.last_activity = time.time()

    def on_progress(self, backend_id, amount):
        with self._lock:
            if backend_id != self.active_backend:
                return
            self.progress = float(amount)
            self.changes += 1
            self.last_activity = time.time()
        callback = self.progress_callback
        if callback is not None:
            try:
                callback(float(amount))
            except Exception:
                pass


class SlicerTools:
    def __init__(self, output_dir_provider):
        self._output_dir_provider = output_dir_provider
        self.watcher = SliceWatcher()
        self.last_model_name = None
        self.last_model_path = None
        self.last_analysis = None    # предпечатная проверка и 5D-совет — для паспорта
        self.last_gcode_path = None
        self.last_gcode_check = None
        self.last_project_path = None
        self.last_plan = None        # ориентация и обоснования из apply_plan
        self.run_context = {}        # агент кладёт сюда модель LLM и текст задачи
        self.cancel_check = None  # агент ставит функцию «нажали ли Стоп», чтобы прерывать долгие ожидания
        self.recipes = None  # RecipeStore, подключает плагин

    # ------------------------------------------------------------------ состояние

    def get_printer_state(self):
        return run_on_main(self._printer_state)

    def _printer_state(self):
        app = _app()
        global_stack = _global_stack()
        machine_manager = app.getMachineManager()
        mode = global_stack.getProperty("printing_mode", "value")
        extruders = []
        for index, stack in enumerate(_extruders(global_stack)):
            extruders.append({
                "index": index,
                "enabled": bool(getattr(stack, "isEnabled", True)),
                "material": stack.material.getName(),
                "material_id": stack.material.getMetaDataEntry("base_file", stack.material.getId()),
                "nozzle": stack.variant.getName(),
            })
        settings = OrderedDict()
        for key in KEY_SETTINGS:
            definition = _definition(global_stack, key)
            if definition is None:
                continue
            settings[key] = _jsonable(_stack_for(global_stack, definition).getProperty(key, "value"))
        user_changes = sorted(set(global_stack.userChanges.getAllKeys()).union(
            *[set(s.userChanges.getAllKeys()) for s in _extruders(global_stack)]))
        return {
            "printer": global_stack.getName(),
            "printer_definition": global_stack.definition.getName(),
            "printing_mode": mode,
            "printing_mode_label": PRINTING_MODES.get(mode, mode),
            "quality_type": machine_manager.activeQualityType,
            "quality_name": machine_manager.activeQualityOrQualityChangesName,
            "extruders": extruders,
            "key_settings": settings,
            "user_changed_settings": user_changes[:12],
            "user_changed_count": len(user_changes),
            "objects": [_node_info(n, i) for i, n in enumerate(_scene_nodes())],
            "slice_state": STATE_NAMES.get(self.watcher.state, "unknown"),
        }

    # ------------------------------------------------------------------ профили и материалы

    def list_quality_profiles(self):
        return run_on_main(self._list_quality)

    def _quality_groups(self):
        return _app().getQualityManager().getQualityGroups(_global_stack())

    def _list_quality(self):
        active = _app().getMachineManager().activeQualityType
        profiles = []
        for quality_type, group in self._quality_groups().items():
            layer_height = None
            try:
                container = group.node_for_global.getContainer() if group.node_for_global else None
                if container is not None:
                    layer_height = _jsonable(container.getProperty("layer_height", "value"))
            except Exception:
                pass
            profiles.append({
                "quality_type": quality_type,
                "name": group.name,
                "available": bool(group.is_available),
                "layer_height": layer_height,
                "active": quality_type == active,
            })
        profiles.sort(key=lambda p: (not p["available"], p["name"]))
        return {"active": active, "profiles": profiles}

    def set_quality_profile(self, quality_type):
        return run_on_main(self._set_quality, quality_type)

    def _set_quality(self, quality_type):
        groups = self._quality_groups()
        group = groups.get(quality_type)
        if group is None:
            text = str(quality_type).strip().lower()
            group = next((g for g in groups.values() if g.name.lower() == text), None)
        if group is None:
            raise ToolError("Профиль «%s» не найден. Доступны: %s" % (
                quality_type, ", ".join("%s (%s)" % (k, g.name) for k, g in groups.items() if g.is_available)))
        if not group.is_available:
            raise ToolError("Профиль «%s» недоступен для текущего материала/сопла" % group.name)
        machine_manager = _app().getMachineManager()
        self._preserving_user_changes(lambda: machine_manager.setQualityGroup(group, no_dialog=True))
        return {"quality_type": machine_manager.activeQualityType, "name": machine_manager.activeQualityOrQualityChangesName}

    def list_materials(self, extruder=0, query=None):
        return run_on_main(self._list_materials, extruder, query)

    def _available_materials(self, extruder):
        global_stack = _global_stack()
        stack = _extruder(global_stack, extruder)
        manager = _app().getMaterialManager()
        return stack, manager.getAvailableMaterialsForMachineExtruder(global_stack, stack) or {}

    def _list_materials(self, extruder=0, query=None):
        stack, available = self._available_materials(extruder)
        active = stack.material.getMetaDataEntry("base_file", stack.material.getId())
        items = []
        for root_id, node in available.items():
            brand = node.getMetaDataEntry("brand", "") or ""
            name = node.getMetaDataEntry("name", root_id) or root_id
            description = " ".join((node.getMetaDataEntry("description", "") or "").split())
            properties = node.getMetaDataEntry("properties", {}) or {}
            score = 0
            if not query and brand != "Stereotech":
                continue
            if query:
                # Любое слово запроса (по основе из 4 букв: «шестерня» найдёт «шестерён»), больше совпадений — выше.
                haystack = " ".join([root_id, name, brand, description]).lower()
                score = sum(1 for word in str(query).lower().split() if word[:4] in haystack)
                if not score:
                    continue
            item = {"id": root_id, "name": name, "brand": brand, "density": properties.get("density"), "_score": score}
            if root_id == active:
                item["active"] = True
            if description:
                item["description"] = description[:170]
            items.append(item)
        items.sort(key=lambda i: (-i["_score"], i["brand"] != "Stereotech", i["name"]))
        for item in items:
            del item["_score"]
        return {"extruder": int(extruder or 0), "active": active, "count": len(items), "materials": items}

    def set_material(self, material_id, extruder=0):
        return run_on_main(self._set_material, material_id, extruder)

    def _set_material(self, material_id, extruder=0):
        _, available = self._available_materials(extruder)
        root_id = self._match_material(available, material_id)
        machine_manager = _app().getMachineManager()
        position = str(int(extruder or 0))
        self._preserving_user_changes(lambda: machine_manager.setMaterialById(position, root_id))
        stack = _extruder(_global_stack(), extruder)
        return {"extruder": int(position), "material": stack.material.getName(),
                "material_id": stack.material.getMetaDataEntry("base_file", stack.material.getId())}

    @staticmethod
    def _match_material(available, wanted):
        if wanted in available:
            return wanted
        text = str(wanted).strip().lower()
        by_name = [k for k, n in available.items() if (n.getMetaDataEntry("name", "") or "").lower() == text]
        if by_name:
            return by_name[0]
        partial = [k for k, n in available.items()
                   if text in k.lower() or text in (n.getMetaDataEntry("name", "") or "").lower()]
        if len(partial) == 1:
            return partial[0]
        raise ToolError("Материал «%s» не найден однозначно. Вызовите list_materials и передайте id." % wanted)

    def _preserving_user_changes(self, action):
        """Смена профиля/материала при наличии пользовательских изменений вызывает модальный диалог
        «сохранить или сбросить». Чтобы его не было, временно убираем изменения и возвращаем их."""
        global_stack = _global_stack()
        saved = []
        for stack in [global_stack] + _extruders(global_stack):
            container = stack.userChanges
            values = {key: container.getProperty(key, "value") for key in container.getAllKeys()}
            saved.append((stack, values))
            container.clear()
        try:
            action()
        finally:
            for stack, values in saved:
                for key, value in values.items():
                    stack.userChanges.setProperty(key, "value", value)

    def set_printing_mode(self, mode):
        return run_on_main(self._set_printing_mode, mode)

    def _set_printing_mode(self, mode):
        global_stack = _global_stack()
        key = str(mode).strip()
        if key not in PRINTING_MODES:
            by_label = {v.lower(): k for k, v in PRINTING_MODES.items()}
            key = by_label.get(key.lower(), key)
        if key not in PRINTING_MODES:
            raise ToolError("Неизвестный режим «%s». Допустимо: %s" % (
                mode, ", ".join("%s (%s)" % (k, v) for k, v in PRINTING_MODES.items())))
        if key != "classic" and not global_stack.getProperty("machine_five_axis", "value"):
            raise ToolError("Текущий принтер не поддерживает 5D-печать")
        global_stack.userChanges.setProperty("printing_mode", "value", key)
        result = {"printing_mode": key, "label": PRINTING_MODES[key]}
        if key != "classic":
            result["base_diameter_mm"] = _jsonable(global_stack.getProperty("cylindrical_mode_base_diameter", "value"))
            result["note"] = ("В 5D деталь печатается вокруг цилиндрической базы в центре стола: ось вращения детали "
                              "должна быть вертикальной и проходить через центр (orient_model goal=axis_vertical). "
                              "Диаметр базы — cylindrical_mode_base_diameter (не больше диаметра отверстия).")
        return result

    # ------------------------------------------------------------------ настройки

    def search_settings(self, query, limit=15):
        return run_on_main(self._search_settings, query, limit)

    def _search_settings(self, query, limit=15):
        global_stack = _global_stack()
        words = [w for w in str(query).lower().split() if w]
        if not words:
            raise ToolError("Пустой запрос")
        scored = []
        for key in global_stack.definition.getAllKeys():
            definition = _definition(global_stack, key)
            if definition is None or definition.type == "category":
                continue
            label = _label(definition)
            haystack = " ".join([key, definition.label or "", label or "", definition.description or ""]).lower()
            score = sum(2 if w in key.lower() or w in (label or "").lower() else 1 for w in words if w in haystack)
            if score:
                scored.append((score, key, definition, label))
        scored.sort(key=lambda item: (-item[0], item[1]))
        results = []
        for _, key, definition, label in scored[:max(1, min(int(limit or 10), 12))]:
            stack = _stack_for(global_stack, definition)
            results.append({
                "key": key,
                "label": label,
                "value": _jsonable(stack.getProperty(key, "value")),
                "unit": definition.unit or "",
                "type": definition.type,
                "enabled": bool(stack.getProperty(key, "enabled")),
                "description": _description(definition)[:120],
            })
        return {"query": query, "results": results}

    def get_settings(self, keys, extruder=None):
        return run_on_main(self._get_settings, keys, extruder)

    def _get_settings(self, keys, extruder=None):
        global_stack = _global_stack()
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        result = OrderedDict()
        for key in keys or []:
            definition = _definition(global_stack, key)
            if definition is None:
                result[key] = {"error": "нет такой настройки — используйте search_settings"}
                continue
            stack = _stack_for(global_stack, definition, extruder)
            entry = {
                "label": _label(definition),
                "description": _description(definition),
                "value": _jsonable(stack.getProperty(key, "value")),
                "unit": definition.unit or "",
                "type": definition.type,
                "per_extruder": bool(definition.settable_per_extruder),
                "enabled": bool(stack.getProperty(key, "enabled")),
            }
            if definition.type == "enum" and definition.options:
                entry["options"] = _jsonable(dict(definition.options))
            problem = _validation(stack, definition)
            if problem:
                entry["validation"] = problem
            result[key] = entry
        return {"settings": result}

    def set_settings(self, settings, extruder=None):
        return run_on_main(self._set_settings, settings, extruder)

    def _set_settings(self, settings, extruder=None):
        if not isinstance(settings, dict) or not settings:
            raise ToolError("settings должен быть объектом вида {\"ключ\": значение}")
        global_stack = _global_stack()
        changed, errors = [], []
        for key, raw in settings.items():
            if key == "printing_mode":
                changed.append({"key": key, "new": self._set_printing_mode(raw)["printing_mode"]})
                continue
            definition = _definition(global_stack, key)
            if definition is None or definition.type == "category":
                errors.append({"key": key, "error": "нет такой настройки — используйте search_settings"})
                continue
            try:
                value = _convert(definition, raw)
            except (TypeError, ValueError) as e:
                errors.append({"key": key, "error": "неверное значение %r: %s" % (raw, e)})
                continue
            if definition.settable_per_extruder:
                stacks = [_extruder(global_stack, extruder)] if extruder is not None else _extruders(global_stack)
            else:
                stacks = [global_stack]
            out_of_range = _range_problem(stacks[0], key, value, _material_id(stacks[0]))
            if out_of_range:
                errors.append({"key": key, "error": out_of_range + " — значение не применено"})
                continue
            old = _jsonable(stacks[0].getProperty(key, "value"))
            for stack in stacks:
                stack.userChanges.setProperty(key, "value", value)
            entry = {"key": key, "label": _label(definition), "old": old,
                     "new": _jsonable(stacks[0].getProperty(key, "value"))}
            problem = _validation(stacks[0], definition)
            if problem:
                entry["validation"] = problem
            changed.append(entry)
        result = {"changed": changed, "errors": errors}
        flow = _flow_check(global_stack)
        if flow:
            result["flow"] = flow
        return result

    def reset_settings(self, keys=None):
        return run_on_main(self._reset_settings, keys)

    def _reset_settings(self, keys=None):
        global_stack = _global_stack()
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        removed = set()
        for stack in [global_stack] + _extruders(global_stack):
            for key in list(stack.userChanges.getAllKeys()):
                if keys and key not in keys:
                    continue
                if not keys and key == "printing_mode":
                    continue  # режим печати сбрасываем только явно
                stack.userChanges.removeInstance(key)
                removed.add(key)
        return {"reset": sorted(removed)}

    def save_profile(self, name):
        return run_on_main(self._save_profile, name)

    def _save_profile(self, name):
        app = _app()
        quality_manager = app.getQualityManager()
        global_stack = _global_stack()
        unique = app.getContainerRegistry().uniqueName(str(name))
        quality_manager.createQualityChanges(unique)
        groups = quality_manager.getQualityChangesGroups(global_stack)
        candidates = groups.values() if isinstance(groups, dict) else groups
        group = next((g for g in candidates if getattr(g, "name", None) == unique), None)
        if group is not None:
            app.getMachineManager().setQualityChangesGroup(group, no_dialog=True)
        return {"profile": unique, "activated": group is not None}

    # ------------------------------------------------------------------ модели и сцена

    def load_model(self, path, clear_scene=True):
        path = _resolve_model_path(path)
        extension = os.path.splitext(path)[1].lower()
        if extension not in (".stl", ".obj", ".3mf"):
            raise ToolError("Поддерживаются STL, OBJ и 3MF")

        done = threading.Event()
        target = os.path.basename(path).lower()

        class _Completion:
            def on_completed(self, file_name):
                if os.path.basename(str(file_name)).lower() == target:
                    done.set()

        completion = _Completion()

        def start():
            app = _app()
            if clear_scene:
                app.deleteAll()
            app.fileCompleted.connect(completion.on_completed)
            app.readLocalFile(QUrl.fromLocalFile(path), True)

        run_on_main(start)
        try:
            if not done.wait(300):
                raise ToolError("Модель не загрузилась за 5 минут")
        finally:
            try:
                run_on_main(lambda: _app().fileCompleted.disconnect(completion.on_completed))
            except Exception:
                pass
        time.sleep(0.5)
        self.last_model_name = os.path.splitext(os.path.basename(path))[0]
        self.last_model_path = path
        self.last_analysis = None
        self.last_plan = None
        return {"loaded": path, "objects": run_on_main(self._objects)}

    def list_objects(self):
        return {"objects": run_on_main(self._objects)}

    def _objects(self):
        return [_node_info(node, i) for i, node in enumerate(_scene_nodes())]

    def analyze_model(self, object=None, overhang_angle=45, load=None, flexible=False):
        node = run_on_main(_find_node, object)
        vertices, indices = run_on_main(_world_mesh, node)
        context = run_on_main(_advisor_context, node)
        analysis = geometry.analyze(vertices, indices, float(overhang_angle or 45))
        revolution = analysis["revolution"]
        current = analysis["current"]
        best = analysis["by_supports"][0]

        health = geometry.mesh_health(analysis["triangles"])
        samples = 200 if analysis["triangle_count"] <= 200000 else 100
        thickness = geometry.wall_thickness(analysis["triangles"], analysis["normals"], analysis["areas"], samples=samples)
        preflight = advisor.preflight(analysis, health, thickness, context["scale_percent"], context["line_width"],
                                      context["density"])
        five_axis = advisor.five_axis(analysis, load, bool(flexible), context["speed"], context["overlap"],
                                      context["density"])

        axial_ratio = revolution["length_along_axis"] / max(revolution["outer_diameter"], 1e-6)
        hints = []
        if current["overhang_percent"] > best["overhang_percent"] + 2:
            hints.append("Поворот уменьшит нависания с %.1f%% до %.1f%% площади (orient_model goal=min_supports)."
                         % (current["overhang_percent"], best["overhang_percent"]))
        if best["overhang_percent"] < 1.0:
            hints.append("Есть ориентация, в которой модель печатается практически без поддержек.")
        if not preflight["ok"]:
            hints.append("Предпечатная проверка нашла ошибки (preflight, level=fail): сначала исправь их или спроси "
                         "пользователя, потом нарезай. Готовое исправление — в поле fix.")

        self.last_analysis = OrderedDict([("object", node.getName()), ("model_path", self.last_model_path),
                                          ("preflight", preflight), ("five_axis", five_axis)])
        return {
            "object": node.getName(),
            "size_mm": geometry.size_to_printer(analysis["size"]),
            "volume_cm3": round(analysis["volume_mm3"] / 1000.0, 2),
            "triangles": analysis["triangle_count"],
            "preflight": preflight,
            "five_axis": five_axis,
            "overhang_angle_deg": float(overhang_angle or 45),
            "current_orientation": geometry.format_metrics(current),
            "best_for_no_supports": [geometry.format_metrics(m) for m in analysis["by_supports"][:2]],
            "revolution": {
                "is_body_of_revolution": revolution["is_body_of_revolution"],
                "symmetry": round(revolution["symmetry"], 3),
                "roundness": round(revolution["roundness"], 3),
                "axis": geometry.to_printer(revolution["axis"]),
                "axis_is_vertical": revolution["axis_is_vertical"],
                "outer_diameter_mm": round(revolution["outer_diameter"], 2),
                "length_along_axis_mm": round(revolution["length_along_axis"], 2),
                "axial_ratio": round(axial_ratio, 2),
                "axis_parallel_share": round(revolution["axis_parallel_share"], 2),
                "through_bore": revolution["through_bore"],
                "bore_diameter_mm": round(revolution["bore_diameter"], 2),
            },
            "hints": hints,
        }

    def orient_model(self, object=None, goal="min_supports"):
        goals = ("min_supports", "max_contact", "axis_vertical")
        if goal not in goals:
            raise ToolError("goal должен быть одним из: %s" % ", ".join(goals))
        node = run_on_main(_find_node, object)
        vertices, indices = run_on_main(_world_mesh, node)
        analysis = geometry.analyze(vertices, indices)
        before = analysis["current"]

        if goal == "axis_vertical":
            axis = analysis["revolution"]["axis"]
            args = (analysis["triangles"], analysis["normals"], analysis["areas"])
            option_a = geometry.orientation_metrics(*(args + (axis,)))
            option_b = geometry.orientation_metrics(*(args + (-axis,)))
            chosen = option_a if option_a["contact_area"] >= option_b["contact_area"] else option_b
        elif goal == "max_contact":
            chosen = analysis["by_contact"][0]
        else:
            chosen = analysis["by_supports"][0]

        down = chosen["down"]
        rotation = Quaternion.rotationTo(Vector(float(down[0]), float(down[1]), float(down[2])).normalized(),
                                         Vector(0, -1, 0))

        def apply():
            _rotate(node, rotation)
            if goal == "axis_vertical":
                _move_center_to(node, 0, 0)
            return _node_info(node)

        info = run_on_main(apply)
        result = {
            "goal": goal,
            "before": geometry.format_metrics(before),
            "after": geometry.format_metrics(chosen),
            "object": info,
        }
        if goal == "axis_vertical":
            result["note"] = "Ось вращения поставлена вертикально, деталь отцентрована на столе (X=0, Y=0)."
        return result

    def rotate_model(self, axis, angle, object=None):
        axis = str(axis).strip().lower()
        if axis not in PRINTER_AXES:
            raise ToolError("axis должен быть x, y или z (оси принтера, z — вверх)")

        def apply():
            node = _find_node(object)
            rotation = Quaternion.fromAngleAxis(float(angle) * 3.141592653589793 / 180.0, PRINTER_AXES[axis])
            _rotate(node, rotation)
            return _node_info(node)

        return {"rotated": {"axis": axis, "angle_deg": float(angle)}, "object": run_on_main(apply)}

    def move_model(self, x=0.0, y=0.0, object=None):
        def apply():
            node = _find_node(object)
            _move_center_to(node, x, y)
            _drop_to_plate(node)
            return _node_info(node)

        return {"object": run_on_main(apply)}

    def scale_model(self, percent, object=None):
        factor = float(percent) / 100.0
        if not 0.01 <= factor <= 100:
            raise ToolError("percent вне разумного диапазона (1–10000)")

        def apply():
            node = _find_node(object)
            ScaleOperation(node, Vector(factor, factor, factor), set_scale=True).push()
            _drop_to_plate(node)
            return _node_info(node)

        return {"object": run_on_main(apply)}

    def arrange_all(self):
        run_on_main(lambda: _app().arrangeAll())
        time.sleep(2.0)
        return {"objects": run_on_main(self._objects)}

    def delete_model(self, object=None, all=False):
        def apply():
            if all:
                _app().deleteAll()
                return "all"
            node = _find_node(object)
            name = node.getName()
            RemoveSceneNodeOperation(node).push()
            return name

        return {"deleted": run_on_main(apply), "objects": run_on_main(self._objects)}

    # ------------------------------------------------------------------ слайсинг

    def slice(self, wait=True, timeout=None):
        if not run_on_main(_scene_nodes):
            raise ToolError("Сцена пуста — нечего нарезать")
        mode = run_on_main(lambda: _global_stack().getProperty("printing_mode", "value"))
        if timeout is None:
            # 5D-движок (Glicer) строит цилиндрические слои в разы дольше обычной нарезки.
            timeout = 600 if mode == "classic" else 2400
        # CuraEngine шлёт прогресс постоянно, а Glicer молчит до конца: 4 минуты тишины в 5D — это
        # обычная работа, и ранний перезапуск обрывал нарезку по кругу.
        stall_limit = 240 if mode == "classic" else 900
        if self.watcher.ensure_connected():
            time.sleep(3.0)  # слежение только началось — недавних изменений мы не видели
        # Запускаем нарезку только когда настройки «осели» — иначе слайсер отменит её или упадёт.
        self.watcher.wait_settled()

        def start():
            backend = _app().getBackend()
            self.watcher.reset(backend.getPluginId())
            backend.forceSlice()
            return backend.getPluginId()

        backend_id = run_on_main(start)
        if not wait:
            return {"started": True, "backend": backend_id}

        started = last_start = time.time()
        restarts = 0
        was_processing = False
        while True:
            if self.cancel_check is not None and self.cancel_check():
                run_on_main(lambda: _app().getBackend().stopSlicing())
                raise ToolError("Слайсинг остановлен пользователем")
            state = self.watcher.state
            if state == BackendState.Done:
                break
            if state == BackendState.Error:
                raise ToolError("Слайсинг завершился ошибкой. Проверьте, что модель внутри области печати "
                                "и настройки без ошибок (get_settings покажет validation).")
            if state == BackendState.Disabled:
                raise ToolError("Слайсинг недоступен: модель вне области печати или нет объектов для печати.")
            if state == BackendState.Processing:
                was_processing = True
            now = time.time()
            if now - started > timeout:
                raise ToolError("Слайсинг не завершился за %d с" % timeout)
            # Нарезку отменили (state вернулся в NotStarted) или она так и не началась — запускаем заново.
            cancelled = was_processing and state == BackendState.NotStarted
            silent = self.watcher.changes == 0 and now - last_start > 10
            # Движок слайсера иногда «замолкает» посреди нарезки: ни прогресса, ни смены состояния.
            stalled = state == BackendState.Processing and now - max(self.watcher.last_activity, last_start) > stall_limit
            if stalled:
                Logger.log("w", "[AIAssistant] Slicing stalled, restarting")
                run_on_main(lambda: _app().getBackend().stopSlicing())
            if (cancelled or silent or stalled) and now - last_start > 3:
                if restarts >= 5:
                    raise ToolError("Слайсер несколько раз отменил нарезку — повторите slice через несколько секунд")
                self.watcher.wait_settled()
                run_on_main(start)
                restarts += 1
                last_start = time.time()
                was_processing = False
            time.sleep(0.25)

        time.sleep(1.0)  # информация о времени и материале приходит сразу после Done
        result = run_on_main(self._slice_result)
        result["backend"] = backend_id
        result["slicing_seconds"] = round(time.time() - started, 1)
        return result

    def get_slice_status(self):
        return {
            "state": STATE_NAMES.get(self.watcher.state, "unknown"),
            "progress_percent": round(self.watcher.progress * 100.0, 1),
            "backend": self.watcher.active_backend,
        }

    def get_slice_result(self):
        return run_on_main(self._slice_result)

    def _slice_result(self):
        info = _app().getPrintInformation()
        duration = info.currentPrintTime
        seconds = int(duration) if duration is not None else None
        weights = [round(float(w), 1) for w in (info.materialWeights or [])]
        lengths = [round(float(l), 2) for l in (info.materialLengths or [])]
        names = [str(n) for n in (info.materialNames or [])]
        # Только реально использованные материалы с подписью: голый массив [0, 168.2] модель читала
        # как «печатает второй экструдер», хотя порядок у PrintInformation свой.
        used = [{"material": names[i] if i < len(names) else "материал %d" % (i + 1),
                 "weight_g": weights[i], "length_m": lengths[i] if i < len(lengths) else None}
                for i in range(len(weights)) if weights[i] > 0]
        return {
            "print_time_seconds": seconds,
            "print_time": _format_duration(seconds),
            "materials_used": used,
            "total_weight_g": round(sum(weights), 1),
            "printing_mode": _global_stack().getProperty("printing_mode", "value"),
        }

    def _target_path(self, name, path, filename):
        if path:
            target = os.path.abspath(os.path.expanduser(str(path)))
        else:
            folder = os.path.join(self._output_dir_provider(), _safe_name(name or self.last_model_name or "model"))
            target = os.path.join(folder, filename)
        directory = os.path.dirname(target)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        return target

    def export_gcode(self, name=None, path=None):
        if is_main_thread():
            raise ToolError("export_gcode нельзя вызывать из главного потока")

        def ready():
            app = _app()
            gcode_dict = getattr(app.getController().getScene(), "gcode_dict", None) or {}
            return bool(gcode_dict.get(app.getMultiBuildPlateModel().activeBuildPlate))

        if not run_on_main(ready):
            raise ToolError("G-code ещё не готов — сначала выполните slice")
        target = self._target_path(name, path, "result.gcode")
        writer = PluginRegistry.getInstance().getPluginObject("GCodeWriter")
        # Пишем из потока агента: миниатюру GCodeWriter сам рисует в главном потоке (call_on_qt_thread)
        # и ждёт его. Вызов из главного потока повесил бы слайсер намертво.
        with open(target, "wt", encoding="utf-8") as stream:
            ok = writer.write(stream, None)
        if not ok:
            raise ToolError("GCodeWriter не смог записать файл: %s" % writer.getInformation())
        result = {"path": target}
        result.update(_check_gcode(target, run_on_main(_gcode_limits)))
        self.last_gcode_path = target
        self.last_gcode_check = result
        return result

    def validate_gcode(self, path=None):
        path = path or self.last_gcode_path
        if not path:
            raise ToolError("Нет пути: сначала export_gcode или передайте path")
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            raise ToolError("Файл не найден: %s" % path)
        result = {"path": path}
        result.update(_check_gcode(path, run_on_main(_gcode_limits)))
        return result

    def save_project(self, name=None, path=None):
        if is_main_thread():
            raise ToolError("save_project нельзя вызывать из главного потока")
        target = self._target_path(name, path, "project.3mf")
        app = _app()
        writer = app.getWorkspaceFileHandler().getWriter("3MFWriter")
        if writer is None:
            raise ToolError("Плагин 3MFWriter недоступен")
        root = app.getController().getScene().getRoot()
        # ThreeMFWorkspaceWriter.write сам переходит в главный поток (call_on_qt_thread) и ждёт завершения.
        with open(target, "wb") as stream:
            ok = writer.write(stream, [root])
        if not ok:
            raise ToolError("Не удалось сохранить проект 3MF")
        self.last_project_path = target
        return {"path": target, "size_kb": round(os.path.getsize(target) / 1024.0, 1)}

    def save_passport(self, name=None, rationale=None):
        """Паспорт рецепта рядом с G-code и 3MF: что, из чего и как напечатано, почему и чем проверено."""
        if is_main_thread():
            raise ToolError("save_passport нельзя вызывать из главного потока")
        if not self.last_gcode_path:
            raise ToolError("Сначала нарежьте и сохраните G-code (apply_plan или export_gcode)")
        reasons = OrderedDict((self.last_plan or {}).get("rationale") or {})
        if isinstance(rationale, dict):
            reasons.update((str(k), str(v)) for k, v in rationale.items() if v)
        state = run_on_main(_passport_state)
        state["result"] = run_on_main(self._slice_result)
        record = passport.build(
            state=state,
            model_path=self.last_model_path,
            analysis=self.last_analysis,
            plan=self.last_plan,
            rationale=reasons,
            gcode_path=self.last_gcode_path,
            gcode_check=self.last_gcode_check,
            project_path=self.last_project_path,
            context=self.run_context,
        )
        target = self._target_path(name, None, "passport.json")
        paths = passport.write(os.path.dirname(target), record)
        return {"path": paths["markdown"], "json_path": paths["json"], "status": record["status"],
                "notes": record["notes"]}

    # ------------------------------------------------------------------ конвейер

    def apply_plan(self, printing_mode, material_id, quality_type=None, orientation="min_supports",
                   settings=None, name=None, object=None, rationale=None):
        """Применяет все решения разом и доводит проект до G-code, 3MF и паспорта рецепта: меньше шагов —
        меньше шансов, что модель застрянет в исследовании."""
        done = OrderedDict()
        self.last_plan = {"orientation": orientation,
                          "rationale": rationale if isinstance(rationale, dict) else {}}
        self.watcher.ensure_connected()
        try:
            done["reset"] = self.reset_settings()["reset"]
            done["mode"] = self.set_printing_mode(printing_mode)["printing_mode"]
            done["material"] = self.set_material(material_id)["material"]
            if quality_type:
                done["quality"] = self.set_quality_profile(quality_type)["name"]
            if orientation and orientation != "none":
                oriented = self.orient_model(object, orientation)
                done["orientation"] = {"goal": orientation, "after": oriented["after"], "object": oriented["object"]}
            if settings:
                applied = self.set_settings(settings)
                done["settings"] = {c["key"]: c["new"] for c in applied["changed"]}
                if applied["errors"]:
                    done["settings_errors"] = applied["errors"]
            flow = run_on_main(lambda: _flow_check(_global_stack()))
            if flow:
                done["flow"] = flow
            done["slice"] = self.slice(wait=True)
            done["gcode"] = self.export_gcode(name=name)
            done["project"] = self.save_project(name=name)
        except ToolError as e:
            raise ToolError("План применён частично (%s), остановился на ошибке: %s"
                            % (", ".join(done.keys()) or "ничего", e))
        gcode = done["gcode"]
        try:
            record = self.save_passport(name=name)
        except Exception as e:  # паспорт не должен ронять уже готовые G-code и 3MF
            Logger.log("w", "[AIAssistant] Passport failed: %s", e)
            record = {"error": "паспорт не сохранён: %s" % e}
        return {
            "applied": {k: v for k, v in done.items() if k not in ("slice", "gcode", "project")},
            "passport_path": record.get("path"),
            "passport_notes": record.get("notes") or record.get("error"),
            "print_time": done["slice"]["print_time"],
            "print_time_seconds": done["slice"]["print_time_seconds"],
            "total_weight_g": done["slice"]["total_weight_g"],
            "printing_mode": done["slice"]["printing_mode"],
            "gcode_path": gcode["path"],
            "gcode_valid": gcode["valid"],
            "gcode_issues": gcode["issues"],
            "gcode_warnings": gcode["warnings"],
            "project_path": done["project"]["path"],
        }

    # ------------------------------------------------------------------ рецепты

    def _recipe_store(self):
        if self.recipes is None:
            raise ToolError("Хранилище рецептов не подключено")
        return self.recipes

    def save_recipe(self, name, description=None, orientation=None):
        store = self._recipe_store()
        snapshot = run_on_main(self._recipe_snapshot)
        plan = self.last_plan or {}
        if plan.get("orientation") in ORIENTATIONS:
            snapshot["orientation"] = plan["orientation"]
        if orientation in ORIENTATIONS:
            snapshot["orientation"] = orientation
        snapshot["rationale"] = dict(plan.get("rationale") or {})  # обоснования уйдут в паспорт при применении
        try:
            recipe = store.put(dict(snapshot, name=name, description=str(description or "")))
        except ValueError as e:
            raise ToolError(str(e))
        return {"saved": recipe, "apply": "/recipe %s" % recipe["name"]}

    def _recipe_snapshot(self):
        global_stack = _global_stack()
        extruder = _extruder(global_stack, 0)
        mode = global_stack.getProperty("printing_mode", "value")
        settings = OrderedDict()
        for stack in (global_stack, extruder):
            for key in sorted(stack.userChanges.getAllKeys()):
                if key != "printing_mode":
                    settings[key] = _jsonable(stack.userChanges.getProperty(key, "value"))
        return {
            "printing_mode": mode,
            "material_id": _material_id(extruder),
            "material": extruder.material.getName(),
            "quality_type": _app().getMachineManager().activeQualityType,
            "orientation": "min_supports" if mode == "classic" else "axis_vertical",
            "settings": settings,
        }

    def list_recipes(self):
        recipes = self._recipe_store().all()
        return {"count": len(recipes), "recipes": [{
            "name": r["name"],
            "description": r.get("description") or "",
            "printing_mode": r.get("printing_mode"),
            "material": r.get("material") or r.get("material_id"),
            "material_id": r.get("material_id"),
            "quality_type": r.get("quality_type"),
            "orientation": r.get("orientation"),
            "settings": r.get("settings") or {},
            "used": r.get("used", 0),
        } for r in recipes]}

    def apply_recipe(self, recipe, name=None, object=None):
        store = self._recipe_store()
        try:
            found = store.get(recipe)
        except ValueError as e:
            raise ToolError(str(e))
        rationale = found.get("rationale") or None
        result = self.apply_plan(found["printing_mode"], found["material_id"], found.get("quality_type"),
                                 found.get("orientation") or "min_supports", found.get("settings") or None,
                                 name, object, rationale)
        store.mark_used(found["name"])
        result["recipe"] = found["name"]
        return result

    # ------------------------------------------------------------------ разбор настроек ученика

    def review_my_settings(self, purpose=None):
        return learning.review(run_on_main(self._review_data), purpose)

    def _review_data(self):
        global_stack = _global_stack()
        extruder = _extruder(global_stack, 0)
        values = OrderedDict()
        for key in REVIEW_KEYS:
            definition = _definition(global_stack, key)
            if definition is None:
                continue
            values[key] = {"label": _label(definition), "unit": definition.unit or "",
                           "value": _jsonable(_stack_for(global_stack, definition).getProperty(key, "value"))}
        user = OrderedDict()
        for stack in (global_stack, extruder):
            for key in sorted(stack.userChanges.getAllKeys()):
                user[key] = _jsonable(stack.userChanges.getProperty(key, "value"))
        problems = []
        for key, value in user.items():
            definition = _definition(global_stack, key)
            if definition is None or key == "printing_mode":
                continue
            stack = _stack_for(global_stack, definition)
            problem = _range_problem(stack, key, value, _material_id(stack))
            if not problem and _validation(stack, definition):
                problem = "слайсер отмечает значение %s как ошибочное" % _jsonable(value)
            if problem:
                problems.append({"key": key, "label": _label(definition), "error": problem})
                values.setdefault(key, {"label": _label(definition), "unit": definition.unit or "",
                                        "value": _jsonable(value)})
        return {
            "mode": global_stack.getProperty("printing_mode", "value"),
            "material_id": _material_id(extruder),
            "material_name": extruder.material.getName(),
            "quality": _app().getMachineManager().activeQualityOrQualityChangesName,
            "values": values,
            "user_changes": user,
            "problems": problems,
            "flow": _flow_check(global_stack),
        }

    # ------------------------------------------------------------------ сравнение вариантов

    def compare_variants(self, variants):
        if not isinstance(variants, list) or not 1 <= len(variants) <= 4:
            raise ToolError("variants — список из 1–4 вариантов")
        snapshot = run_on_main(self._snapshot)
        rows = []
        try:
            for index, variant in enumerate(variants):
                if not isinstance(variant, dict):
                    raise ToolError("Каждый вариант — объект {label, quality_type?, material_id?, settings?}")
                label = variant.get("label") or "Вариант %d" % (index + 1)

                def apply(variant=variant):
                    self._restore(snapshot)
                    if variant.get("quality_type"):
                        self._set_quality(variant["quality_type"])
                    if variant.get("material_id"):
                        self._set_material(variant["material_id"], variant.get("extruder", 0))
                    if variant.get("settings"):
                        self._set_settings(variant["settings"], variant.get("extruder"))

                run_on_main(apply)
                outcome = self.slice(wait=True)
                rows.append({
                    "label": label,
                    "print_time": outcome["print_time"],
                    "print_time_seconds": outcome["print_time_seconds"],
                    "total_weight_g": outcome["total_weight_g"],
                })
        finally:
            run_on_main(self._restore, snapshot)
        timed = [r for r in rows if r["print_time_seconds"] is not None]
        return {
            "variants": rows,
            "fastest": min(timed, key=lambda r: r["print_time_seconds"])["label"] if timed else None,
            "lightest": min(rows, key=lambda r: r["total_weight_g"])["label"] if rows else None,
            "note": "Исходные настройки восстановлены. Примените выбранный вариант и снова запустите slice.",
        }

    def _snapshot(self):
        global_stack = _global_stack()
        stacks = [global_stack] + _extruders(global_stack)
        return {
            "quality_type": _app().getMachineManager().activeQualityType,
            "materials": [s.material.getMetaDataEntry("base_file", s.material.getId()) for s in _extruders(global_stack)],
            "user": [{k: s.userChanges.getProperty(k, "value") for k in s.userChanges.getAllKeys()} for s in stacks],
        }

    def _restore(self, snapshot):
        global_stack = _global_stack()
        stacks = [global_stack] + _extruders(global_stack)
        for stack in stacks:
            stack.userChanges.clear()
        machine_manager = _app().getMachineManager()
        groups = self._quality_groups()
        if snapshot["quality_type"] in groups and machine_manager.activeQualityType != snapshot["quality_type"]:
            machine_manager.setQualityGroup(groups[snapshot["quality_type"]], no_dialog=True)
        for index, material_id in enumerate(snapshot["materials"]):
            stack = _extruders(global_stack)[index]
            if stack.material.getMetaDataEntry("base_file", stack.material.getId()) != material_id:
                machine_manager.setMaterialById(str(index), material_id)
        for stack, values in zip(stacks, snapshot["user"]):
            for key, value in values.items():
                stack.userChanges.setProperty(key, "value", value)


def _resolve_model_path(path):
    """Абсолютный путь — как есть. Относительный или неточный — ищем файл в «Документах», на рабочем столе
    и в «Загрузках»: сначала по совпадению хвоста пути, потом по имени файла."""
    raw = os.path.expanduser(str(path).strip().strip('"').strip("'"))
    if os.path.isabs(raw) and os.path.isfile(raw):
        return raw
    wanted_tail = os.path.normcase(os.path.normpath(raw)).lstrip("." + os.sep)
    wanted_name = os.path.normcase(os.path.basename(raw))
    by_name = None
    seen = 0
    home = os.path.expanduser("~")
    roots = [os.path.join(tempfile.gettempdir(), "ste_ai_uploads"), os.path.join(home, "Documents"),
             os.path.join(home, "Desktop"), os.path.join(home, "Downloads")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for folder, dirs, files in os.walk(root):
            if folder.count(os.sep) - base_depth >= 6:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
            for name in files:
                seen += 1
                if os.path.normcase(name) != wanted_name:
                    continue
                full = os.path.join(folder, name)
                if os.path.normcase(full).endswith(wanted_tail):
                    return full
                by_name = by_name or full
            if seen > 200000:
                break
    if by_name:
        return by_name
    raise ToolError("Файл не найден: %s. Не подбирай пути наугад — попроси пользователя прикрепить модель "
                    "скрепкой в чате (вложения загружаются автоматически) или прислать полный путь." % raw)


def _passport_state():
    """Состояние слайсера для паспорта (главный поток): принтер, версии, профиль, материал, изменённые настройки."""
    app = _app()
    global_stack = _global_stack()
    machine_manager = app.getMachineManager()
    extruder = _extruder(global_stack, 0)
    material = extruder.material
    properties = material.getMetaDataEntry("properties", {}) or {}
    settings, seen = [], set()
    for stack in [global_stack] + _extruders(global_stack):
        for key in sorted(stack.userChanges.getAllKeys()):
            if key in seen:
                continue
            seen.add(key)
            definition = _definition(global_stack, key)
            settings.append(OrderedDict([
                ("key", key),
                ("label", _label(definition) if definition is not None else key),
                ("value", _jsonable(stack.getProperty(key, "value"))),
                ("unit", (definition.unit or "") if definition is not None else ""),
            ]))
    try:
        version = app.getVersion()
    except Exception:
        version = None
    mode = global_stack.getProperty("printing_mode", "value")
    return {
        "printer": global_stack.getName(),
        "printer_definition": global_stack.definition.getName(),
        "slicer_version": version,
        "printing_mode": mode,
        "printing_mode_label": PRINTING_MODES.get(mode, mode),
        "quality": machine_manager.activeQualityOrQualityChangesName,
        "quality_type": machine_manager.activeQualityType,
        "material": OrderedDict([
            ("id", material.getMetaDataEntry("base_file", material.getId())),
            ("name", material.getName()),
            ("brand", material.getMetaDataEntry("brand", "") or ""),
            ("density", properties.get("density")),
            ("description", " ".join((material.getMetaDataEntry("description", "") or "").split())[:300]),
        ]),
        "nozzle": extruder.variant.getName(),
        "settings": settings,
        "objects": [_node_info(n, i) for i, n in enumerate(_scene_nodes())],
    }


def _gcode_limits():
    """Границы для проверки G-code: стол, режим и температуры из стека настроек (главный поток)."""
    global_stack = _global_stack()
    extruder = _extruder(global_stack, 0)

    def number(stack, key, prop="value"):
        return _number(stack.getProperty(key, prop))

    return {
        "width": number(global_stack, "machine_width"),
        "depth": number(global_stack, "machine_depth"),
        "height": number(global_stack, "machine_height"),
        "center_is_zero": bool(global_stack.getProperty("machine_center_is_zero", "value")),
        "printing_mode": global_stack.getProperty("printing_mode", "value"),
        "nozzle_temp": number(extruder, "material_print_temperature"),
        "nozzle_max": number(extruder, "material_print_temperature", "maximum_value"),
        "bed_max": number(global_stack, "material_bed_temperature", "maximum_value"),
    }


def _axis_values(line):
    values = {}
    for token in line.split(";", 1)[0].split()[1:]:
        if token[:1] in "XYZAC":
            try:
                values[token[0]] = float(token[1:])
            except ValueError:
                pass
    return values


def _check_gcode(path, limits=None):
    """Проверка G-code: движения и слои, границы стола, температуры, поворотные оси в 5D, парковка.
    issues — то, что делает файл непригодным; warnings — что стоит посмотреть глазами."""
    moves = layers = 0
    has_flavor = False
    header, tail = [], []
    low, high = {}, {}
    nozzle, bed = [], []
    with open(path, "rt", encoding="utf-8", errors="replace") as stream:
        for number, line in enumerate(stream):
            # Миниатюра ;PREVIEW — килобайты base64, в ответе модели она только съедает лимит запроса.
            if number < 40 and line.startswith(";") and not line.startswith(";PREVIEW") and len(header) < 12:
                header.append(line.strip()[:120])
            if line.startswith(("G0 ", "G1 ", "G2 ", "G3 ")):
                moves += 1
                for axis, value in _axis_values(line).items():
                    if value < low.get(axis, value + 1):
                        low[axis] = value
                    if value > high.get(axis, value - 1):
                        high[axis] = value
            elif line.startswith(";LAYER:"):
                layers += 1
            elif line.startswith(("M104", "M109", "M140", "M190")):
                match = re.search(r"\bS(\d+(?:\.\d+)?)", line)
                if match:
                    (bed if line.startswith(("M140", "M190")) else nozzle).append(float(match.group(1)))
            elif line.startswith(";FLAVOR"):
                has_flavor = True
            if not line.startswith(";SETTING_3"):
                tail.append(line.strip())
                if len(tail) > 60:
                    del tail[0]
    issues, warnings = [], []
    if moves == 0:
        issues.append("нет команд перемещения")
    if not any(t > 0 for t in nozzle):
        issues.append("нет команд нагрева сопла (M104/M109)")
    limits = limits or {}
    mode = limits.get("printing_mode") or "classic"
    if mode == "classic" and limits.get("width") and limits.get("depth"):
        for axis, size in (("X", limits["width"]), ("Y", limits["depth"])):
            start, end = (-size / 2.0, size / 2.0) if limits.get("center_is_zero") else (0.0, size)
            if axis in low and (low[axis] < start - 1 or high[axis] > end + 1):
                issues.append("%s выходит за стол: %.1f…%.1f при допустимых %.0f…%.0f мм"
                              % (axis, low[axis], high[axis], start, end))
    if limits.get("height") and high.get("Z", 0) > limits["height"] + 1:
        issues.append("Z до %.1f мм выше рабочей высоты %.0f мм" % (high["Z"], limits["height"]))
    if mode != "classic" and not ("A" in low or "C" in low):
        issues.append("режим 5D, но нет движений поворотных осей A/C")
    if nozzle and limits.get("nozzle_max") and max(nozzle) > limits["nozzle_max"]:
        issues.append("температура сопла %.0f °C выше предела %.0f °C" % (max(nozzle), limits["nozzle_max"]))
    if nozzle and limits.get("nozzle_temp") and abs(max(nozzle) - limits["nozzle_temp"]) > 15:
        warnings.append("температура сопла %.0f °C, а в профиле материала %.0f °C" % (max(nozzle), limits["nozzle_temp"]))
    if bed and limits.get("bed_max") and max(bed) > limits["bed_max"]:
        issues.append("температура стола %.0f °C выше предела %.0f °C" % (max(bed), limits["bed_max"]))
    ending = " ".join(tail)
    if not re.search(r"\b(END|G28|M84)\b", ending):
        warnings.append("в конце нет парковки (END, G28 или M84)")
    if not re.search(r"\bM104 S0\b", ending):
        warnings.append("в конце не выключается нагрев сопла (M104 S0)")
    axes = {axis: [round(low[axis], 1), round(high[axis], 1)] for axis in sorted(low)}
    return {
        "size_kb": round(os.path.getsize(path) / 1024.0, 1),
        "moves": moves,
        "layers": layers,
        "has_flavor_header": has_flavor,
        "axes_range": axes,
        "nozzle_temp_max": max(nozzle) if nozzle else None,
        "bed_temp_max": max(bed) if bed else None,
        "header": header,
        "issues": issues,
        "warnings": warnings,
        "valid": not issues,
    }


def register_tools(registry, tools, learning_tools=None):
    """Описания инструментов для MCP и LLM. Описания — на русском, ключи настроек — как в слайсере.
    modes — в каких режимах чата модель видит инструмент (во внешнем MCP доступны все)."""
    obj = {"type": "string", "description": "Номер объекта из list_objects или часть имени. Можно не указывать, если объект один."}

    registry.add("get_printer_state", "Состояние слайсера",
                 "Текущий принтер, режим печати (3D/5D), профиль качества, материалы экструдеров, ключевые настройки, объекты на столе.",
                 {}, tools.get_printer_state)
    registry.add("list_quality_profiles", "Профили качества",
                 "Список профилей качества (quality_type, имя, доступность, высота слоя).",
                 {}, tools.list_quality_profiles)
    registry.add("set_quality_profile", "Выбор профиля",
                 "Выбрать профиль качества по quality_type (или имени) из list_quality_profiles. Пользовательские изменения сохраняются.",
                 {"quality_type": {"type": "string"}}, tools.set_quality_profile, required=["quality_type"])
    registry.add("list_materials", "Список материалов",
                 "Материалы Stereotech для экструдера с описанием и плотностью. query — поиск по словам среди всех материалов (например «PA», «гибкий», «шестер»).",
                 {"extruder": {"type": "integer", "default": 0}, "query": {"type": "string"}}, tools.list_materials)
    registry.add("set_material", "Выбор материала",
                 "Назначить материал экструдеру по id из list_materials.",
                 {"material_id": {"type": "string"}, "extruder": {"type": "integer", "default": 0}},
                 tools.set_material, required=["material_id"])
    registry.add("set_printing_mode", "Режим 3D/5D",
                 "Переключить режим печати: classic (3D), cylindrical (5D Spiral), cylindrical_full (5D).",
                 {"mode": {"type": "string", "enum": list(PRINTING_MODES.keys())}}, tools.set_printing_mode, required=["mode"])
    registry.add("search_settings", "Поиск настроек",
                 "Найти ключи настроек по словам (рус/англ): например «infill», «стенк», «support», «adhesion».",
                 {"query": {"type": "string"}, "limit": {"type": "integer", "default": 15}}, tools.search_settings, required=["query"])
    registry.add("get_settings", "Чтение настроек",
                 "Значения настроек по ключам: подпись, описание из слайсера (на что влияет), единицы, допустимые варианты, ошибки валидации. Используй и для справочных ответов.",
                 {"keys": {"type": "array", "items": {"type": "string"}}, "extruder": {"type": "integer"}},
                 tools.get_settings, required=["keys"])
    registry.add("set_settings", "Изменение настроек",
                 "Изменить настройки: {\"ключ\": значение}. Настройки экструдера пишутся во все экструдеры, если extruder не указан.",
                 {"settings": {"type": "object", "additionalProperties": True}, "extruder": {"type": "integer"}},
                 tools.set_settings, required=["settings"])
    registry.add("reset_settings", "Сброс настроек",
                 "Сбросить пользовательские изменения (все или перечисленные ключи). Режим печати без явного указания не сбрасывается.",
                 {"keys": {"type": "array", "items": {"type": "string"}}}, tools.reset_settings)
    registry.add("save_profile", "Сохранение профиля",
                 "Сохранить текущие изменения как новый профиль и активировать его. Только по просьбе пользователя.",
                 {"name": {"type": "string"}}, tools.save_profile, required=["name"])
    registry.add("load_model", "Загрузка модели",
                 "Загрузить STL/OBJ/3MF в сцену по пути к файлу. По умолчанию очищает стол.",
                 {"path": {"type": "string"}, "clear_scene": {"type": "boolean", "default": True}},
                 tools.load_model, required=["path"])
    registry.add("list_objects", "Объекты на столе",
                 "Список объектов: номер, имя, габариты (мм), положение центра, масштаб.",
                 {}, tools.list_objects)
    registry.add("analyze_model", "Анализ геометрии",
                 "Анализ модели: предпечатная проверка (единицы, целостность сетки, тонкие стенки, отверстия, рабочая "
                 "область), нависания и площадь опоры, тело вращения и отверстие, 5D-советник (five_axis): вердикт "
                 "3D/5D по баллам с объяснением, диаметр базы. load — главная нагрузка из задачи, flexible — нужен "
                 "гибкий материал.",
                 {"object": obj, "overhang_angle": {"type": "number", "default": 45},
                  "load": {"type": "string", "enum": list(advisor.LOADS.keys()),
                           "description": "torsion — кручение, pressure — внутреннее давление, rolling — качение и "
                                          "трение по поверхности, centrifugal — центробежная нагрузка на лопатки, "
                                          "bending — изгиб зубьев и рычагов, static — статика или декор"},
                  "flexible": {"type": "boolean", "default": False}},
                 tools.analyze_model)
    registry.add("orient_model", "Авто-ориентация",
                 "Автоматически повернуть модель: min_supports — минимум нависаний; max_contact — максимум площади на столе; "
                 "axis_vertical — ось вращения вертикально и по центру стола (для 5D).",
                 {"object": obj, "goal": {"type": "string", "enum": ["min_supports", "max_contact", "axis_vertical"]}},
                 tools.orient_model)
    registry.add("rotate_model", "Поворот",
                 "Повернуть модель на angle градусов вокруг оси принтера x, y или z (z — вверх). Модель остаётся на столе.",
                 {"axis": {"type": "string", "enum": ["x", "y", "z"]}, "angle": {"type": "number"}, "object": obj},
                 tools.rotate_model, required=["axis", "angle"])
    registry.add("move_model", "Перемещение",
                 "Поставить центр модели в точку (x, y) в мм относительно центра стола.",
                 {"x": {"type": "number"}, "y": {"type": "number"}, "object": obj}, tools.move_model)
    registry.add("scale_model", "Масштаб",
                 "Масштабировать модель, percent — процент от исходного размера (100 = без изменений).",
                 {"percent": {"type": "number"}, "object": obj}, tools.scale_model, required=["percent"])
    registry.add("arrange_all", "Авторасстановка",
                 "Автоматически расставить все объекты на столе (для 3D). В 5D вместо этого центрируйте деталь.",
                 {}, tools.arrange_all)
    registry.add("delete_model", "Удаление",
                 "Удалить объект со стола или все объекты (all=true).",
                 {"object": obj, "all": {"type": "boolean", "default": False}}, tools.delete_model)
    registry.add("slice", "Слайсинг",
                 "Запустить слайсинг. По умолчанию ждёт окончания и возвращает время печати и расход материала.",
                 {"wait": {"type": "boolean", "default": True}}, tools.slice)
    registry.add("get_slice_status", "Статус слайсинга",
                 "Состояние и прогресс слайсинга.", {}, tools.get_slice_status)
    registry.add("get_slice_result", "Результат слайсинга",
                 "Время печати и расход материала последнего слайсинга.", {}, tools.get_slice_result)
    registry.add("export_gcode", "Экспорт G-code",
                 "Сохранить G-code (по умолчанию <папка результатов>/<name>/result.gcode) и проверить его корректность.",
                 {"name": {"type": "string", "description": "Имя папки, обычно имя модели"}, "path": {"type": "string"}},
                 tools.export_gcode)
    registry.add("validate_gcode", "Проверка G-code",
                 "Проверить G-code по текущим настройкам: стол, высота, температуры сопла и стола, поворотные оси A/C в 5D, "
                 "парковка в конце. Без path — последний экспортированный файл.",
                 {"path": {"type": "string"}}, tools.validate_gcode)
    registry.add("save_project", "Сохранение 3MF",
                 "Сохранить проект 3MF со всеми настройками (по умолчанию <папка результатов>/<name>/project.3mf).",
                 {"name": {"type": "string"}, "path": {"type": "string"}}, tools.save_project)
    registry.add("apply_plan", "Применение плана",
                 "ГЛАВНЫЙ ШАГ. Одним вызовом: сброс старых настроек, режим, материал, профиль, ориентация, ключевые "
                 "настройки, слайсинг, экспорт G-code и сохранение 3MF. Вызывай после analyze_model и выбора материала.",
                 {"printing_mode": {"type": "string", "enum": list(PRINTING_MODES.keys())},
                  "material_id": {"type": "string", "description": "id из list_materials"},
                  "quality_type": {"type": "string", "description": "quality_type из list_quality_profiles: high, normal, fast, draft…"},
                  "orientation": {"type": "string", "enum": ["min_supports", "max_contact", "axis_vertical", "none"]},
                  "settings": {"type": "object", "additionalProperties": True,
                               "description": "ключевые настройки, например {\"wall_line_count\": 5, \"infill_sparse_density\": 60}"},
                  "name": {"type": "string", "description": "имя папки результатов, например 01_gear"},
                  "object": obj,
                  "rationale": {"type": "object", "additionalProperties": True,
                                "description": "обоснования для паспорта рецепта, коротко: {\"mode\": …, \"material\": …, "
                                               "\"quality\": …, \"orientation\": …, \"<ключ настройки>\": …}"}},
                 tools.apply_plan, required=["printing_mode", "material_id"])
    registry.add("save_passport", "Паспорт рецепта",
                 "Сохранить паспорт рецепта (passport.md и passport.json рядом с G-code): решения и их обоснования, "
                 "изменённые настройки, предпечатная проверка, 5D-совет, время и масса, SHA-256 файлов, поля для "
                 "технолога. apply_plan делает это сам — вызывай после ручных правок и повторного экспорта.",
                 {"name": {"type": "string", "description": "имя папки результатов"},
                  "rationale": {"type": "object", "additionalProperties": True,
                                "description": "обоснования: {\"mode\": …, \"<ключ настройки>\": …}"}},
                 tools.save_passport)
    registry.add("compare_variants", "Сравнение вариантов",
                 "Нарезать 1–4 варианта настроек и сравнить время и расход материала. "
                 "Вариант: {label, quality_type?, material_id?, settings?}. Исходное состояние восстанавливается.",
                 {"variants": {"type": "array", "items": {"type": "object"}}}, tools.compare_variants, required=["variants"])
    registry.add("review_my_settings", "Разбор настроек",
                 "Проверить настройки, которые пользователь выставил сам: диапазоны слайсера и паспорта материала, "
                 "рекомендации по назначению детали, расход Q, совместимость режима и материала. Возвращает находки "
                 "с советами — объясни каждую. Ничего не меняет.",
                 {"purpose": {"type": "string", "enum": list(learning.PURPOSES.keys()),
                              "description": "назначение детали; без него — функциональная (или гибкая для TPU)"}},
                 tools.review_my_settings)
    registry.add("save_recipe", "Сохранение рецепта",
                 "Сохранить текущие режим, материал, профиль, ориентацию и изменённые настройки как рецепт для "
                 "повторного применения одним вызовом. Только по просьбе пользователя.",
                 {"name": {"type": "string"}, "description": {"type": "string"},
                  "orientation": {"type": "string", "enum": ORIENTATIONS}},
                 tools.save_recipe, required=["name"], modes=("engineer", "pro"))
    registry.add("list_recipes", "Список рецептов",
                 "Сохранённые рецепты: режим, материал, профиль, ориентация, настройки, сколько раз применялись.",
                 {}, tools.list_recipes, modes=("engineer", "pro"))
    registry.add("apply_recipe", "Применение рецепта",
                 "Применить рецепт к загруженной модели как apply_plan: сброс, режим, материал, профиль, ориентация, "
                 "настройки, слайсинг, G-code и 3MF.",
                 {"recipe": {"type": "string", "description": "имя рецепта из list_recipes"},
                  "name": {"type": "string", "description": "имя папки результатов"}, "object": obj},
                 tools.apply_recipe, required=["recipe"], modes=("engineer", "pro"))
    if learning_tools is not None:
        def quiz(topic=None, count=2):
            try:
                return learning_tools.quiz(topic, count)
            except ValueError as e:
                raise ToolError(str(e))

        registry.add("quiz", "Квиз",
                     "Показать пользователю 1–3 вопроса с кнопками по теме задачи. Ответ проверяет чат, "
                     "не повторяй вопросы текстом.",
                     {"topic": {"type": "string", "enum": list(learning.TOPICS.keys())},
                      "count": {"type": "integer", "default": 2}},
                     quiz, modes=("learning", "novice", "engineer"))
