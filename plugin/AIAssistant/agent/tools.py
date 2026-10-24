# Инструменты над API STE Slicer. Публичные методы можно вызывать из любого потока:
# всё, что трогает сцену и настройки, уходит в главный поток через run_on_main.
# Методы с префиксом _ предполагают, что уже выполняются в главном потоке.

import json
import os
import re
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

from . import geometry
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


class _BackendListener:
    """Держит сильную ссылку на обработчики: сигналы Uranium хранят слабые ссылки."""

    def __init__(self, watcher, backend_id):
        self._watcher = watcher
        self._backend_id = backend_id

    def on_state(self, state):
        self._watcher.on_state(self._backend_id, state)

    def on_progress(self, amount):
        self._watcher.on_progress(self._backend_id, amount)


class SliceWatcher:
    def __init__(self):
        self._lock = threading.Lock()
        self._listeners = {}
        self.active_backend = None
        self.state = None
        self.progress = 0.0
        self.changes = 0
        self.progress_callback = None

    def ensure_connected(self):
        run_on_main(self._connect)

    def _connect(self):
        for backend_id, backend in _app().getBackendManager().getBackends().items():
            if backend_id in self._listeners:
                continue
            listener = _BackendListener(self, backend_id)
            backend.backendStateChange.connect(listener.on_state)
            backend.processingProgress.connect(listener.on_progress)
            self._listeners[backend_id] = listener

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

    def on_progress(self, backend_id, amount):
        with self._lock:
            if backend_id != self.active_backend:
                return
            self.progress = float(amount)
            self.changes += 1
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
        self.cancel_check = None  # агент ставит функцию «нажали ли Стоп», чтобы прерывать долгие ожидания

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
            old = _jsonable(stacks[0].getProperty(key, "value"))
            for stack in stacks:
                stack.userChanges.setProperty(key, "value", value)
            entry = {"key": key, "label": _label(definition), "old": old,
                     "new": _jsonable(stacks[0].getProperty(key, "value"))}
            problem = _validation(stacks[0], definition)
            if problem:
                entry["validation"] = problem
            changed.append(entry)
        return {"changed": changed, "errors": errors}

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
        return {"loaded": path, "objects": run_on_main(self._objects)}

    def list_objects(self):
        return {"objects": run_on_main(self._objects)}

    def _objects(self):
        return [_node_info(node, i) for i, node in enumerate(_scene_nodes())]

    def analyze_model(self, object=None, overhang_angle=45):
        node = run_on_main(_find_node, object)
        vertices, indices = run_on_main(_world_mesh, node)
        analysis = geometry.analyze(vertices, indices, float(overhang_angle or 45))
        revolution = analysis["revolution"]
        current = analysis["current"]
        best = analysis["by_supports"][0]

        axial_ratio = revolution["length_along_axis"] / max(revolution["outer_diameter"], 1e-6)
        hints = []
        if revolution["is_body_of_revolution"]:
            bore = (", cylindrical_mode_base_diameter не больше ⌀%.1f мм отверстия" % revolution["bore_diameter"]
                    if revolution["through_bore"] else "")
            if axial_ratio >= 0.35:
                hints.append("Тело вращения, длина вдоль оси = %.2f диаметра — кандидат на 5D (cylindrical_full) для "
                             "жёсткого материала: ось вертикально по центру стола%s." % (axial_ratio, bore))
            else:
                hints.append("Плоский диск (длина вдоль оси = %.2f диаметра) — обычно 3D, осью вертикально, "
                             "рабочие элементы (зубья, лопасти) в плоскости стола." % axial_ratio)
        elif revolution["axis_parallel_share"] >= 0.55:
            hints.append("Деталь вытянута вдоль выраженной оси (вал, кулачок, ось). Если она вращается или работает "
                         "на кручение и скольжение по профилю — кандидат на 5D (cylindrical_full), ось вертикально.")
        if current["overhang_percent"] > best["overhang_percent"] + 2:
            hints.append("Поворот уменьшит нависания с %.1f%% до %.1f%% площади (orient_model goal=min_supports)."
                         % (current["overhang_percent"], best["overhang_percent"]))
        if best["overhang_percent"] < 1.0:
            hints.append("Есть ориентация, в которой модель печатается практически без поддержек.")

        return {
            "object": node.getName(),
            "size_mm": geometry.size_to_printer(analysis["size"]),
            "volume_cm3": round(analysis["volume_mm3"] / 1000.0, 2),
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

    def slice(self, wait=True, timeout=600):
        if not run_on_main(_scene_nodes):
            raise ToolError("Сцена пуста — нечего нарезать")
        self.watcher.ensure_connected()
        # После смены настроек слайсер пересчитывает их с задержкой и отменяет уже начатую нарезку —
        # даём изменениям «осесть» перед запуском.
        time.sleep(1.5)

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
            if (cancelled or silent) and now - last_start > 3:
                if restarts >= 5:
                    raise ToolError("Слайсер несколько раз отменил нарезку — повторите slice через несколько секунд")
                time.sleep(1.0)
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
        return {
            "print_time_seconds": seconds,
            "print_time": _format_duration(seconds),
            "material_weight_g": weights,
            "material_length_m": lengths,
            "material_names": names,
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
        result.update(_check_gcode(target))
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
        return {"path": target, "size_kb": round(os.path.getsize(target) / 1024.0, 1)}

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
    for root in [os.path.join(home, "Documents"), os.path.join(home, "Desktop"), os.path.join(home, "Downloads")]:
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


def _check_gcode(path):
    """Быстрая проверка корректности G-code: есть ли движения, слои, температуры."""
    moves = layers = 0
    has_temperature = has_flavor = False
    header = []
    with open(path, "rt", encoding="utf-8", errors="replace") as stream:
        for number, line in enumerate(stream):
            if number < 40 and line.startswith(";") and len(header) < 12:
                header.append(line.strip())
            if line.startswith(("G0 ", "G1 ", "G2 ", "G3 ")):
                moves += 1
            elif line.startswith(";LAYER:"):
                layers += 1
            elif line.startswith(("M104", "M109")):
                has_temperature = True
            elif line.startswith(";FLAVOR"):
                has_flavor = True
    issues = []
    if moves == 0:
        issues.append("нет команд перемещения")
    if not has_temperature:
        issues.append("нет команд нагрева сопла (M104/M109)")
    return {
        "size_kb": round(os.path.getsize(path) / 1024.0, 1),
        "moves": moves,
        "layers": layers,
        "has_flavor_header": has_flavor,
        "header": header,
        "issues": issues,
        "valid": not issues,
    }


def register_tools(registry, tools):
    """Описания инструментов для MCP и LLM. Описания — на русском, ключи настроек — как в слайсере."""
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
                 "Анализ модели: габариты, объём, нависания в текущей и лучших ориентациях, площадь опоры, "
                 "признаки тела вращения и сквозного отверстия (подсказка для 5D).",
                 {"object": obj, "overhang_angle": {"type": "number", "default": 45}}, tools.analyze_model)
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
    registry.add("save_project", "Сохранение 3MF",
                 "Сохранить проект 3MF со всеми настройками (по умолчанию <папка результатов>/<name>/project.3mf).",
                 {"name": {"type": "string"}, "path": {"type": "string"}}, tools.save_project)
    registry.add("compare_variants", "Сравнение вариантов",
                 "Нарезать 1–4 варианта настроек и сравнить время и расход материала. "
                 "Вариант: {label, quality_type?, material_id?, settings?}. Исходное состояние восстанавливается.",
                 {"variants": {"type": "array", "items": {"type": "object"}}}, tools.compare_variants, required=["variants"])
