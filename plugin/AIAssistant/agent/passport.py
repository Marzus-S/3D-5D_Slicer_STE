# Паспорт рецепта печати: что, из чего, на чём и с какими настройками напечатано, почему так решено
# и чем проверено. Пишется рядом с G-code и 3MF: passport.json — для программ, passport.md — для людей.
# Без API слайсера: данные собирает tools.save_passport, здесь только сборка и запись.

import hashlib
import json
import os
import time
from collections import OrderedDict

FORMAT = "ste-ai-recipe/1"
STATUS = "черновик — требует проверки технологом"
DECISION_KEYS = OrderedDict([("mode", "Режим"), ("material", "Материал"), ("quality", "Профиль"),
                             ("orientation", "Ориентация")])
TO_FILL = ["Партия и дата изготовления материала", "Сушка перед печатью (режим, время)", "Принтер (серийный номер)",
           "Проверил и утвердил (ФИО, дата)", "Результат печати: годна / брак, замеры, дефекты"]
LEVEL_MARKS = {"ok": "✓", "info": "ℹ", "warn": "⚠", "fail": "✗"}


def sha256(path):
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plugin_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin.json"),
                  encoding="utf-8") as stream:
            return json.load(stream).get("version")
    except Exception:
        return None


def _file(path):
    if not path:
        return None
    return OrderedDict([("path", path), ("sha256", sha256(path))])


def build(state, model_path=None, analysis=None, plan=None, rationale=None, gcode_path=None, gcode_check=None,
          project_path=None, context=None, now=None):
    rationale = OrderedDict(rationale or {})
    context = context or {}
    analysis = analysis or {}
    objects = state.get("objects") or []
    part = OrderedDict([("name", objects[0]["name"] if objects else None)])
    if objects:
        part["size_mm"] = objects[0].get("size_mm")
        part["scale_percent"] = objects[0].get("scale_percent")
    part["model"] = _file(model_path)

    decisions = OrderedDict([
        ("mode", "%s (%s)" % (state.get("printing_mode_label"), state.get("printing_mode"))),
        ("material", "%s — %s" % (state["material"]["name"], state["material"]["id"])),
        ("quality", "%s (%s)" % (state.get("quality"), state.get("quality_type"))),
        ("orientation", (plan or {}).get("orientation")),
    ])

    settings = []
    for item in state.get("settings") or []:
        entry = OrderedDict(item)
        if rationale.get(item["key"]):
            entry["why"] = rationale[item["key"]]
        settings.append(entry)

    notes = []
    five_axis = analysis.get("five_axis") or {}
    preflight = (analysis.get("preflight") or {}).get("checks") or []
    for check in preflight:
        if check["id"] == "hole" or check["level"] in ("warn", "fail"):
            notes.append(check["text"])
    if five_axis.get("verdict") and state.get("printing_mode"):
        advised = five_axis.get("mode")
        chosen = state["printing_mode"]
        if advised and (advised == "classic") != (chosen == "classic"):
            notes.append("режим отличается от рекомендации 5D-советника (%s) — обоснование обязательно" % advised)
    if gcode_check and not gcode_check.get("valid", True):
        notes.append("G-code не прошёл проверку: %s" % "; ".join(gcode_check.get("issues") or []))

    record = OrderedDict([
        ("format", FORMAT),
        ("created", time.strftime("%Y-%m-%d %H:%M", time.localtime(now))),
        ("status", STATUS),
        ("part", part),
        ("task", (context.get("task") or "").strip()[:3000] or None),
        ("decisions", decisions),
        ("rationale", OrderedDict((k, v) for k, v in rationale.items() if k in DECISION_KEYS)),
        ("settings", settings),
        ("five_axis_advice", five_axis or None),
        ("preflight", preflight),
        ("result", state.get("result")),
        ("files", OrderedDict([("gcode", _file(gcode_path)), ("project", _file(project_path))])),
        ("gcode_check", OrderedDict((k, gcode_check.get(k)) for k in ("valid", "layers", "moves", "issues", "warnings"))
         if gcode_check else None),
        ("notes", notes),
        ("software", OrderedDict([
            ("ste_slicer", state.get("slicer_version")),
            ("plugin", plugin_version()),
            ("llm_model", context.get("llm_model")),
            ("printer", state.get("printer")),
            ("printer_definition", state.get("printer_definition")),
            ("nozzle", state.get("nozzle")),
        ])),
        ("to_fill", OrderedDict((field, "") for field in TO_FILL)),
    ])
    return record


def _cell(value):
    text = "—" if value in (None, "") else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown(record):
    part = record["part"]
    lines = ["# Паспорт рецепта печати: %s" % _cell(part.get("name")), "",
             "Создан %s · **Статус:** %s" % (record["created"], record["status"]), ""]
    if record.get("task"):
        lines += ["## Задача", "", "> " + record["task"].replace("\n", "\n> "), ""]

    lines += ["## Решения", "", "| Что | Решение | Почему |", "|---|---|---|"]
    for key, title in DECISION_KEYS.items():
        lines.append("| %s | %s | %s |" % (title, _cell(record["decisions"].get(key)), _cell(record["rationale"].get(key))))
    lines.append("")

    advice = record.get("five_axis_advice")
    if advice:
        verdict = {"5d": "5D", "3d": "3D", "either": "оба режима допустимы"}.get(advice.get("verdict"), advice.get("verdict"))
        lines += ["## 5D-советник: %s%s" % (verdict, " (балл %s)" % advice["score"] if "score" in advice else ""), ""]
        for blocker in advice.get("blockers") or []:
            lines.append("- ✗ %s" % blocker)
        for factor in advice.get("factors") or []:
            lines.append("- %+d — %s" % (factor["points"], factor["why"]))
        if advice.get("base_diameter_mm"):
            lines.append("- база ⌀%s мм — %s" % (advice["base_diameter_mm"], advice.get("base_reason", "")))
        if advice.get("evidence"):
            lines.append("- данные о выигрыше: %s" % advice["evidence"])
        lines.append("")

    lines += ["## Изменённые настройки", "", "| Настройка | Ключ | Значение | Почему |", "|---|---|---|---|"]
    for item in record["settings"]:
        value = "%s %s" % (_cell(item["value"]), item.get("unit") or "")
        lines.append("| %s | `%s` | %s | %s |" % (_cell(item["label"]), item["key"], value.strip(), _cell(item.get("why"))))
    if not record["settings"]:
        lines.append("| — | — | профиль без изменений | — |")
    lines.append("")

    if record["preflight"]:
        lines += ["## Предпечатная проверка", ""]
        lines += ["- %s %s" % (LEVEL_MARKS.get(c["level"], "·"), c["text"]) for c in record["preflight"]]
        lines.append("")

    result = record.get("result") or {}
    check = record.get("gcode_check") or {}
    lines += ["## Результат нарезки", "",
              "- Время печати: %s" % _cell(result.get("print_time")),
              "- Материал: %s г" % _cell(result.get("total_weight_g"))]
    if check:
        lines.append("- Проверка G-code: %s, слоёв %s%s" % (
            "пройдена" if check.get("valid") else "НЕ пройдена", _cell(check.get("layers")),
            "; замечания: " + "; ".join(check.get("warnings")) if check.get("warnings") else ""))
    lines.append("")

    if record["notes"]:
        lines += ["## Важно", ""] + ["- %s" % note for note in record["notes"]] + [""]

    lines += ["## Файлы и контрольные суммы SHA-256", "", "| Файл | Путь | SHA-256 |", "|---|---|---|"]
    for title, item in (("Модель", part.get("model")), ("G-code", record["files"].get("gcode")),
                        ("Проект 3MF", record["files"].get("project"))):
        if item:
            lines.append("| %s | `%s` | `%s` |" % (title, item["path"], item["sha256"] or "файл не найден"))
    lines.append("")

    software = record["software"]
    lines += ["## Программы", "",
              "STE Slicer %s · AI Assistant %s · LLM %s · %s, сопло %s" % (
                  _cell(software.get("ste_slicer")), _cell(software.get("plugin")), _cell(software.get("llm_model")),
                  _cell(software.get("printer")), _cell(software.get("nozzle"))), ""]

    lines += ["## Заполняет технолог", "", "| Поле | Значение |", "|---|---|"]
    lines += ["| %s | |" % field for field in record["to_fill"]]
    lines.append("")
    return "\n".join(lines)


def write(folder, record):
    if not os.path.isdir(folder):
        os.makedirs(folder)
    json_path = os.path.join(folder, "passport.json")
    markdown_path = os.path.join(folder, "passport.md")
    with open(json_path, "w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    with open(markdown_path, "w", encoding="utf-8") as stream:
        stream.write(to_markdown(record))
    return {"json": json_path, "markdown": markdown_path}
