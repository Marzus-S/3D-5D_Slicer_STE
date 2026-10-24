# Быстрые команды чата («/infill 40», «/mat pa6», «/recipe шестерня»). Выполняются без LLM: разбор строки
# и прямой вызов инструментов через тот же MCP-обработчик. Ответ — короткий текст для ленты чата.

import shlex
from collections import OrderedDict

MODE_ALIASES = {"3d": "classic", "classic": "classic", "5d": "cylindrical_full", "cylindrical_full": "cylindrical_full",
                "spiral": "cylindrical", "5ds": "cylindrical", "cylindrical": "cylindrical"}
ORIENT_ALIASES = {"min": "min_supports", "supports": "min_supports", "опора": "max_contact", "max": "max_contact",
                  "contact": "max_contact", "ось": "axis_vertical", "axis": "axis_vertical", "5d": "axis_vertical"}
SWITCH = {"on": True, "вкл": True, "да": True, "1": True, "off": False, "выкл": False, "нет": False, "0": False}


class CommandError(Exception):
    pass


def is_command(text):
    return str(text or "").lstrip().startswith("/")


def _split(text):
    try:
        parts = shlex.split(text.strip()[1:])
    except ValueError:
        parts = text.strip()[1:].split()
    if not parts:
        raise CommandError("Пустая команда. Список: /help")
    return parts[0].lower(), parts[1:]


def _number(args, usage):
    if len(args) != 1:
        raise CommandError("Использование: " + usage)
    try:
        return float(args[0].replace(",", ".").rstrip("%"))
    except ValueError:
        raise CommandError("Нужно число. Использование: " + usage)


def _settings_reply(payload):
    lines = []
    for change in payload.get("changed") or []:
        old = change.get("old")
        lines.append("**%s** (`%s`): %s → %s" % (change.get("label") or change["key"], change["key"],
                                               "—" if old is None else old, change.get("new")))
    for error in payload.get("errors") or []:
        lines.append("⚠ `%s`: %s" % (error["key"], error["error"]))
    flow = payload.get("flow") or {}
    if flow.get("warning"):
        lines.append("⚠ " + flow["warning"])
    return "\n".join(lines) or "Ничего не изменилось"


def _set(run, settings):
    ok, payload = run("set_settings", {"settings": settings})
    return _settings_reply(payload) if ok else _error(payload)


def _error(payload):
    return "⚠ " + str((payload or {}).get("error") or payload)


def _plan_reply(payload):
    text = "Готово: **%s**, %s г" % (payload.get("print_time") or "—", payload.get("total_weight_g"))
    if payload.get("recipe"):
        text = "Рецепт «%s». " % payload["recipe"] + text
    applied = payload.get("applied") or {}
    if applied.get("settings_errors"):
        text += "\n⚠ Не применены: " + ", ".join(e["key"] for e in applied["settings_errors"])
    if payload.get("gcode_valid") is False:
        text += "\n⚠ G-code: " + "; ".join(payload.get("gcode_issues") or [])
    return text


# ---------------------------------------------------------------- обработчики: (run, args) → текст ответа

def cmd_mode(run, args):
    if len(args) != 1 or args[0].lower() not in MODE_ALIASES:
        raise CommandError("Использование: /mode 3d | 5d | spiral")
    ok, payload = run("set_printing_mode", {"mode": MODE_ALIASES[args[0].lower()]})
    return "Режим: **%s**" % payload["label"] if ok else _error(payload)


def cmd_mat(run, args):
    if not args:
        raise CommandError("Использование: /mat <часть id или названия>, например /mat pa6")
    query = " ".join(args)
    ok, payload = run("set_material", {"material_id": query})
    if ok:
        return "Материал: **%s** (`%s`)" % (payload["material"], payload["material_id"])
    ok, found = run("list_materials", {"query": query})
    options = [m["id"] for m in (found.get("materials") or [])[:6]] if ok else []
    if not options:
        return _error(payload)
    return "Под «%s» подходит несколько материалов, уточните:\nВарианты: %s" % (
        query, " | ".join("/mat " + o for o in options))


def cmd_profile(run, args):
    if not args:
        raise CommandError("Использование: /profile fine | normal | draft …")
    ok, payload = run("set_quality_profile", {"quality_type": " ".join(args)})
    return "Профиль: **%s**" % payload["name"] if ok else _error(payload)


def _setting(key, usage):
    def handler(run, args):
        value = _number(args, usage)
        return _set(run, {key: int(value) if value == int(value) else value})
    return handler


def cmd_layers(run, args):
    value = int(_number(args, "/layers 6 — сплошные слои сверху и снизу"))
    return _set(run, {"top_layers": value, "bottom_layers": value})


def cmd_supports(run, args):
    if len(args) != 1 or args[0].lower() not in SWITCH:
        raise CommandError("Использование: /supports on | off")
    return _set(run, {"support_enable": SWITCH[args[0].lower()]})


def cmd_adhesion(run, args):
    if len(args) != 1:
        raise CommandError("Использование: /adhesion brim | raft | skirt | none")
    return _set(run, {"adhesion_type": args[0].lower()})


def cmd_set(run, args):
    settings = OrderedDict()
    for item in args:
        if "=" not in item:
            raise CommandError("Использование: /set ключ=значение [ключ=значение …]")
        key, value = item.split("=", 1)
        settings[key.strip()] = value.strip()
    if not settings:
        raise CommandError("Использование: /set ключ=значение [ключ=значение …]")
    return _set(run, settings)


def cmd_orient(run, args):
    goal = ORIENT_ALIASES.get(args[0].lower(), args[0]) if args else "min_supports"
    ok, payload = run("orient_model", {"goal": goal})
    return "Ориентация: %s" % goal if ok else _error(payload)


def cmd_reset(run, args):
    ok, payload = run("reset_settings", {"keys": args} if args else {})
    if not ok:
        return _error(payload)
    return "Сброшено: " + (", ".join("`%s`" % k for k in payload["reset"]) or "нечего сбрасывать")


def cmd_slice(run, args):
    ok, payload = run("slice", {})
    if not ok:
        return _error(payload)
    return "Нарезано: **%s**, %s г" % (payload.get("print_time") or "—", payload.get("total_weight_g"))


def cmd_export(run, args):
    ok, payload = run("export_gcode", {"name": args[0]} if args else {})
    if not ok:
        return _error(payload)
    text = "G-code: `%s`" % payload.get("path")
    if payload.get("valid") is False:
        text += "\n⚠ " + "; ".join(payload.get("issues") or [])
    return text


def cmd_save(run, args):
    ok, payload = run("save_project", {"name": args[0]} if args else {})
    return "Проект 3MF: `%s`" % payload.get("path") if ok else _error(payload)


def cmd_state(run, args):
    ok, state = run("get_printer_state", {})
    if not ok:
        return _error(state)
    extruder = (state.get("extruders") or [{}])[0]
    rows = ["| Параметр | Значение |", "| --- | --- |",
            "| Режим | %s |" % state.get("printing_mode_label"),
            "| Материал | %s |" % extruder.get("material"),
            "| Профиль | %s |" % state.get("quality_name")]
    for key, value in (state.get("key_settings") or {}).items():
        if key != "printing_mode":
            rows.append("| `%s` | %s |" % (key, value))
    return "\n".join(rows)


def cmd_recipes(run, args):
    ok, payload = run("list_recipes", {})
    if not ok:
        return _error(payload)
    if not payload["recipes"]:
        return "Рецептов пока нет. Настройте деталь и сохраните: `/saverecipe имя`"
    rows = ["| Рецепт | Режим | Материал | Профиль | Применён |", "| --- | --- | --- | --- | --- |"]
    for r in payload["recipes"]:
        rows.append("| %s | %s | %s | %s | %d |" % (r["name"], r["printing_mode"], r["material"], r["quality_type"],
                                                   r["used"]))
    return "\n".join(rows) + "\n\nПрименить: `/recipe имя [папка]`"


def cmd_recipe(run, args):
    if not args:
        return cmd_recipes(run, args)
    call = {"recipe": args[0]}
    if len(args) > 1:
        call["name"] = args[1]
    ok, payload = run("apply_recipe", call)
    return _plan_reply(payload) if ok else _error(payload)


def cmd_saverecipe(run, args):
    if not args:
        raise CommandError("Использование: /saverecipe имя [описание]")
    call = {"name": args[0]}
    if len(args) > 1:
        call["description"] = " ".join(args[1:])
    ok, payload = run("save_recipe", call)
    if not ok:
        return _error(payload)
    recipe = payload["saved"]
    return "Рецепт «%s» сохранён: %s, %s, профиль %s, настроек: %d. Применить: `%s`" % (
        recipe["name"], recipe["printing_mode"], recipe.get("material") or recipe["material_id"],
        recipe.get("quality_type"), len(recipe.get("settings") or {}), payload["apply"])


SEVERITY_ICONS = {"ошибка": "⛔", "предупреждение": "⚠", "совет": "💡"}
LOAD_ALIASES = {"кручение": "torsion", "давление": "pressure", "качение": "rolling", "центробежная": "centrifugal",
                "изгиб": "bending", "статика": "static", "декор": "static"}
VERDICTS = {"5d": "печатать в 5D", "3d": "печатать в 3D", "either": "подходят оба режима"}


def cmd_analyze(run, args):
    call = {}
    for arg in args:
        text = arg.lower()
        if text in ("гибкая", "flexible", "tpu"):
            call["flexible"] = True
        else:
            call["load"] = LOAD_ALIASES.get(text, text)
    ok, payload = run("analyze_model", call)
    if not ok:
        return _error(payload)
    preflight = payload.get("preflight") or {}
    advice = payload.get("five_axis") or {}
    lines = []
    if advice.get("verdict"):
        line = "**5D-советник: %s**" % VERDICTS.get(advice["verdict"], advice["verdict"])
        if advice.get("score") is not None:
            line += " (балл %s)" % advice["score"]
        if advice.get("base_diameter_mm"):
            line += ", база ⌀%s мм" % advice["base_diameter_mm"]
        lines.append(line)
        for blocker in advice.get("blockers") or []:
            lines.append("- ✗ %s" % blocker)
        for factor in sorted(advice.get("factors") or [], key=lambda f: -abs(f["points"]))[:3]:
            lines.append("- %+d — %s" % (factor["points"], factor["why"]))
        if advice.get("decide"):
            lines.append(advice["decide"])
        if advice.get("evidence"):
            lines.append("*Выигрыш 5D: %s*" % advice["evidence"])
    if preflight.get("summary"):
        lines.append("\nПредпечатная проверка: %s" % preflight["summary"])
    return "\n".join(lines) or "Анализ выполнен"


def cmd_matinfo(run, args):
    ok, payload = run("material_info", {"material": " ".join(args)} if args else {})
    if not ok:
        return _error(payload)
    marks = dict((s["key"], i + 1) for i, s in enumerate(payload["sources"]))
    lines = ["**%s** (%s) — как напечатано" % (payload["label"], payload["material"])]
    for prop in payload["properties"]:
        lines.append("- %s: %s %s [%d]" % (prop["label"], _num(prop["value"]), prop["unit"], marks[prop["source"]]))
    z = payload["z_ratio"]
    lines.append("- По Z: %s [%d]" % (z["note"], marks[z["source"]]))
    if payload.get("service_temp_c"):
        low, high = payload["service_temp_c"]["value"]
        lines.append("- Рабочая температура: от %d до +%d °C [%d]" % (low, high, marks["printed"]))
    if payload.get("drying"):
        dry = payload["drying"]
        lines.append("- Сушка: %d–%d °C, %d–%d ч%s [%d]" % (dry["temp_c"][0], dry["temp_c"][1], dry["hours"][0],
                     dry["hours"][1], ", материал гигроскопичен" if payload["hygroscopic"] else "",
                     marks[dry["source"]]))
    if payload.get("printblock"):
        lines.append("- Принтблок 2: %s" % payload["printblock"]["note"])
    if payload.get("filled_note"):
        lines.append("- %s" % payload["filled_note"])
    lines.append("")
    for i, source in enumerate(payload["sources"]):
        lines.append("[%d] %s" % (i + 1, "[%s](%s)" % (source["title"], source["url"]) if source["url"]
                                  else source["title"]))
    return "\n".join(lines)


def _num(value):
    return ("%g" % value).replace(".", ",")


def cmd_passport(run, args):
    ok, payload = run("save_passport", {"name": args[0]} if args else {})
    return "Паспорт рецепта: `%s`" % payload.get("path") if ok else _error(payload)


def cmd_check(run, args):
    ok, payload = run("review_my_settings", {"purpose": args[0]} if args else {})
    if not ok:
        return _error(payload)
    lines = ["**Разбор настроек** (%s, %s): %s" % (payload.get("material"), payload.get("printing_mode"),
                                                    payload["summary"])]
    for f in payload["findings"]:
        lines.append("- %s **%s**: %s. %s" % (SEVERITY_ICONS.get(f["severity"], "•"), f["label"], f["problem"],
                                              f["advice"]))
    if payload.get("note"):
        lines.append("\n" + payload["note"])
    return "\n".join(lines)


def cmd_quiz(run, args):
    ok, payload = run("quiz", {"topic": args[0]} if args else {})
    return "" if ok else _error(payload)


# имя → (обработчик, использование, описание)
COMMANDS = OrderedDict([
    ("help", (None, "/help", "список команд")),
    ("state", (cmd_state, "/state", "текущие режим, материал, профиль и ключевые настройки")),
    ("mode", (cmd_mode, "/mode 3d|5d|spiral", "режим печати")),
    ("mat", (cmd_mat, "/mat pa6", "материал по части id или названия")),
    ("profile", (cmd_profile, "/profile fine", "профиль качества")),
    ("infill", (_setting("infill_sparse_density", "/infill 40"), "/infill 40", "заполнение, %")),
    ("walls", (_setting("wall_line_count", "/walls 5"), "/walls 5", "число стенок")),
    ("layers", (cmd_layers, "/layers 6", "сплошные слои сверху и снизу")),
    ("speed", (_setting("speed_print", "/speed 40"), "/speed 40", "скорость печати, мм/с")),
    ("supports", (cmd_supports, "/supports on|off", "поддержки")),
    ("adhesion", (cmd_adhesion, "/adhesion brim|raft|skirt|none", "прилипание к столу")),
    ("set", (cmd_set, "/set ключ=значение …", "любые настройки по ключу")),
    ("orient", (cmd_orient, "/orient min|опора|ось", "авто-ориентация модели")),
    ("reset", (cmd_reset, "/reset [ключи]", "сбросить изменения настроек")),
    ("slice", (cmd_slice, "/slice", "нарезать")),
    ("export", (cmd_export, "/export [папка]", "сохранить и проверить G-code")),
    ("save", (cmd_save, "/save [папка]", "сохранить проект 3MF")),
    ("recipes", (cmd_recipes, "/recipes", "список рецептов")),
    ("recipe", (cmd_recipe, "/recipe имя [папка]", "применить рецепт: настройки, нарезка, G-code и 3MF")),
    ("saverecipe", (cmd_saverecipe, "/saverecipe имя [описание]", "сохранить текущие настройки как рецепт")),
    ("analyze", (cmd_analyze, "/analyze [нагрузка] [гибкая]",
                 "предпечатная проверка и 5D-советник: кручение, давление, качение, изгиб…")),
    ("matinfo", (cmd_matinfo, "/matinfo [материал]",
                 "свойства «как напечатано», сушка и рабочая температура с источниками")),
    ("passport", (cmd_passport, "/passport [папка]", "паспорт рецепта после ручных правок")),
    ("check", (cmd_check, "/check [decor|functional|loaded|sealed|flexible]", "разбор моих настроек")),
    ("quiz", (cmd_quiz, "/quiz [тема]", "проверить себя: вопросы с кнопками")),
])
ALIASES = {"props": "matinfo", "свойства": "matinfo", "material": "mat", "материал": "mat", "q": "quiz", "r": "recipe", "s": "slice", "?": "help"}


def catalog():
    return [{"name": name, "usage": usage, "description": description}
            for name, (_, usage, description) in COMMANDS.items()]


def help_text():
    return "**Быстрые команды** (выполняются сразу, без нейросети):\n" + "\n".join(
        "- `%s` — %s" % (usage, description) for _, usage, description in COMMANDS.values())


def execute(text, run):
    """run(tool, args) → (ok, payload). Возвращает текст ответа; CommandError — ошибка ввода."""
    name, args = _split(text)
    name = ALIASES.get(name, name)
    if name not in COMMANDS:
        raise CommandError("Нет команды /%s. Список: /help" % name)
    handler = COMMANDS[name][0]
    if handler is None:
        return help_text()
    return handler(run, args)
