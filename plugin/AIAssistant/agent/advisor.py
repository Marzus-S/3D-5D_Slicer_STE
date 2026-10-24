# Советник поверх анализа геометрии: предпечатная проверка файла и выбор 3D/5D по баллам.
# Чистые функции над результатом geometry.analyze — без API слайсера, поэтому проверяются тестами.

import math
from collections import OrderedDict

import numpy

from . import geometry, knowledge

# Нагрузка из ТЗ → баллы за 5D. Цилиндрические слои лежат вдоль окружного и осевого направлений,
# граница между слоями — радиальная. Выгодно то, что нагружает деталь вдоль цилиндра, невыгодно — радиальное.
LOADS = OrderedDict([
    ("torsion", ("кручение", 2, "напряжения кручения идут по винтовым линиям на цилиндре — вдоль слоёв 5D")),
    ("pressure", ("внутреннее давление", 2, "окружные напряжения лежат в цилиндрическом слое, а не поперёк плоских")),
    ("rolling", ("качение, трение по наружной поверхности", 1, "сплошная наружная поверхность без ступенек слоёв")),
    ("centrifugal", ("центробежная нагрузка на лопатки", -1, "растяжение лопаток радиальное — поперёк слоёв 5D")),
    ("bending", ("изгиб зубьев, рычагов, кронштейнов", -2,
                 "напряжение у корня зуба радиальное — поперёк слоёв 5D; плоские слои «лёжа» держат его лучше")),
    ("static", ("статика, лёгкая нагрузка, декор", 0, "прочность не решает, выбор — по поддержкам и поверхности")),
])

# Поле допуска IT12 по ГОСТ 25346 (ISO 286): (верхняя граница интервала размеров, мм; допуск, мм).
IT12 = [(3, 0.10), (6, 0.12), (10, 0.15), (18, 0.18), (30, 0.21), (50, 0.25), (80, 0.30), (120, 0.35),
        (180, 0.40), (250, 0.46), (315, 0.52), (400, 0.57), (500, 0.63)]

DENSE_MESH = 300000  # треугольников: выше этого 5D-нарезка (булевы операции + Glicer) заметно дольше
MIN_BASE = 5.0       # cylindrical_mode_base_diameter: ниже 5 мм слайсер предупреждает, минимум 4


def it12(size):
    for upper, tolerance in IT12:
        if size <= upper:
            return tolerance
    return IT12[-1][1]


def _check(checks, level, check_id, text, fix=None):
    item = OrderedDict([("id", check_id), ("level", level), ("text", text)])
    if fix:
        item["fix"] = fix
    checks.append(item)


def _fmt_size(size):
    return "%s×%s×%s мм" % tuple(_num(v) for v in size)


def _num(value, digits=1):
    text = ("%." + str(digits) + "f") % value
    return text.rstrip("0").rstrip(".") if "." in text else text


def preflight(analysis, health, thickness, scale_percent=100.0, line_width=0.4, density=None, limits=None):
    """Проверки до выбора режима: единицы, целостность сетки, тонкие стенки, отверстия, рабочая область, масса.
    level: ok — норма, info — к сведению, warn — стоит посмотреть, fail — печатать в таком виде нельзя."""
    limits = limits or knowledge.PRINTER_LIMITS
    checks = []
    size = [float(analysis["size"][0]), float(analysis["size"][2]), float(analysis["size"][1])]
    largest = max(size)
    area = sorted(limits["area_3d_mm"])

    # Единицы: в STL их нет, модель из CAD в дюймах или сантиметрах приходит в 25,4 или 10 раз меньше.
    if largest < 5.0:
        fixes = []
        for factor, unit in ((25.4, "дюймы"), (10.0, "сантиметры"), (1000.0, "метры")):
            scaled = sorted(v * factor for v in size)
            if all(s <= a for s, a in zip(scaled, area)) and scaled[-1] >= 5.0:
                fixes.append({"unit": unit, "tool": "scale_model",
                              "args": {"percent": round(scale_percent * factor, 2)},
                              "size_after_mm": [round(v * factor, 1) for v in size]})
        text = "габарит %s — для детали подозрительно мало, похоже, файл экспортирован не в мм" % _fmt_size(size)
        if fixes:
            text += "; ×%s (%s) даст %s" % (_num(fixes[0]["args"]["percent"] / scale_percent),
                                              fixes[0]["unit"], _fmt_size(fixes[0]["size_after_mm"]))
        _check(checks, "fail", "units", text, fixes[0] if fixes else None)
    elif any(s > a for s, a in zip(sorted(size), area)):
        _check(checks, "fail", "units", "габарит %s не помещается в область 3D %s ни в одной ориентации: проверьте "
                                        "единицы экспорта (нужны мм) или разделите деталь" % (_fmt_size(size), _fmt_size(area)))
    else:
        _check(checks, "ok", "units", "габарит %s — похоже на миллиметры, деталь помещается в область печати" % _fmt_size(size))

    # Целостность сетки.
    problems = []
    if health["open_edges"]:
        problems.append("%d открытых рёбер (дыры в поверхности)" % health["open_edges"])
    if health["nonmanifold_edges"]:
        problems.append("%d рёбер с тремя и больше гранями" % health["nonmanifold_edges"])
    if problems:
        level = "fail" if health["open_edges"] > 50 else "warn"
        _check(checks, level, "mesh", "сетка не замкнута: %s — слайсер может потерять стенки; переэкспортируйте "
                                      "из CAD или почините (Netfabb, Meshmixer)" % ", ".join(problems))
    else:
        _check(checks, "ok", "mesh", "сетка замкнута, без лишних рёбер")
    if health.get("bodies") and health["bodies"] > 1:
        _check(checks, "warn", "bodies", "в файле %d отдельных тел — если это одна деталь, части не соединены "
                                         "и будут слабо держаться друг за друга" % health["bodies"])
    if analysis.get("inverted"):
        _check(checks, "warn", "normals", "нормали вывернуты внутрь (отрицательный объём) — лучше переэкспортировать")

    # Тонкие стенки: меньше двух периметров — слабо и с пропусками, меньше одной линии — не напечатается.
    finite = thickness[numpy.isfinite(thickness)] if len(thickness) else thickness
    two_walls = 2.0 * line_width
    if len(finite) and finite.min() < limits["min_rib_mm"]:
        _check(checks, "warn", "walls", "есть элементы толщиной ≈%s мм — тоньше минимального ребра %s мм, "
                                        "сопло их не пропечатает" % (_num(finite.min(), 2), _num(limits["min_rib_mm"])))
    elif len(finite) and finite.min() < two_walls:
        share = 100.0 * (finite < two_walls).sum() / len(thickness)
        _check(checks, "warn", "walls", "стенки ≈%s мм тоньше двух периметров (%s мм) на ≈%s%% поверхности — слабые места; "
                                        "утолщите в CAD или уменьшите ширину линии"
               % (_num(finite.min(), 2), _num(two_walls, 2), _num(max(share, 1.0), 0)))
    else:
        _check(checks, "ok", "walls", "стенок тоньше двух периметров (%s мм) не найдено (%d точек)" % (_num(two_walls, 2), len(thickness)))

    # Отверстие по оси: минимальный диаметр принтера и точность для посадок.
    revolution = analysis["revolution"]
    if revolution["through_bore"]:
        bore = revolution["bore_diameter"]
        if bore < limits["min_hole_mm"]:
            _check(checks, "warn", "hole", "отверстие ⌀%s мм меньше минимального для принтера (%s мм) — сверлите после печати"
                   % (_num(bore, 2), _num(limits["min_hole_mm"])))
        else:
            _check(checks, "info", "hole", "отверстие ⌀%s мм: точность принтера %s — поле допуска %s мм; FDM-отверстия "
                                           "выходят меньше на 0,1–0,4 мм, под вал или подшипник заложите развёртку"
                   % (_num(bore, 2), limits["tolerance_grade"], _num(it12(bore), 2)))

    if density:
        mass = analysis["volume_mm3"] / 1000.0 * float(density)
        if mass > limits["mass_3d_g"]:
            _check(checks, "fail", "mass", "сплошная деталь весит ≈%s г — больше предела стола %s г"
                   % (_num(mass, 0), _num(limits["mass_3d_g"], 0)))
    if analysis["triangle_count"] > DENSE_MESH:
        _check(checks, "info", "triangles", "плотная сетка: %d треугольников — нарезка, особенно 5D, будет дольше"
               % analysis["triangle_count"])

    failed = sum(1 for c in checks if c["level"] == "fail")
    warned = sum(1 for c in checks if c["level"] == "warn")
    return OrderedDict([("ok", failed == 0), ("summary", "ошибки — %d, предупреждения — %d, всего пунктов — %d"
                                                         % (failed, warned, len(checks))), ("checks", checks)])


def stable_3d(analysis):
    """Лучшая для 3D ориентация, в которой деталь реально стоит на столе: колесо «на ребре» даёт мало
    нависаний, но касается стола линией — с ним 5D сравнивать нечестно."""
    candidates = analysis["by_supports"]
    top = max(m["contact_area"] for m in candidates)
    return next((m for m in candidates if m["contact_area"] >= 0.2 * top), candidates[0])


def base_diameter(revolution):
    """Диаметр цилиндрической базы 5D: по осевому отверстию, иначе небольшой — чтобы нагруженная
    наружная зона попала в цилиндрические слои, а ось C у центра не крутилась слишком быстро."""
    if revolution["through_bore"] and revolution["bore_diameter"] >= MIN_BASE:
        value = math.floor(revolution["bore_diameter"] * 10.0) / 10.0
        return value, "по отверстию ⌀%s мм: 5D-слои начинаются от его стенки, посадку не портят" % _num(revolution["bore_diameter"], 2)
    if revolution["through_bore"]:
        return MIN_BASE, ("отверстие ⌀%s мм меньше минимальной базы %s мм — у центра будет 3D-ядро, отверстие "
                          "лучше досверлить" % (_num(revolution["bore_diameter"], 2), _num(MIN_BASE)))
    value = min(10.0, max(6.0, round(0.2 * revolution["outer_diameter"] * 2.0) / 2.0))
    return value, "отверстия нет: база ≈20 % диаметра (6–10 мм), наружная зона уходит в цилиндрические слои"


def five_axis(analysis, load=None, flexible=False, speed=30.0, overlap=0.0, density=None, limits=None):
    """Стоит ли печатать в 5D (cylindrical_full): сумма баллов с объяснением каждого, диаметр базы и цифры."""
    limits = limits or knowledge.PRINTER_LIMITS
    revolution = analysis["revolution"]
    factors = []

    def factor(points, text):
        factors.append(OrderedDict([("points", points), ("why", text)]))

    blockers = []
    if revolution["is_body_of_revolution"]:
        factor(2, "тело вращения (круглость %s, симметрия %s)" % (_num(revolution["roundness"], 2), _num(revolution["symmetry"], 2)))
    elif revolution["axis_parallel_share"] >= 0.55 and revolution["symmetry"] >= 0.3:
        # Симметрия сечения отличает вал или ось со звёздами (≈0,5) от плоской пластины или бруска (≈0,04).
        factor(1, "выраженная ось: %d%% поверхности параллельно ей (вал, кулачок)" % round(100 * revolution["axis_parallel_share"]))
    else:
        blockers.append("нет оси вращения — цилиндрические слои не ложатся по форме детали")
    if flexible:
        blockers.append("гибкий материал: гофры и тонкие оболочки печатают в 3D; гибкий протектор на жёсткой втулке "
                        "в 5D — решение технолога")

    diameter = revolution["outer_diameter"]
    length = revolution["length_along_axis"]
    ratio = length / max(diameter, 1e-6)
    if ratio >= 1.0:
        factor(2, "длинное тело (L = %s·D): в 3D стоя слои поперёк оси, лёжа — поддержки" % _num(ratio, 2))
    elif ratio >= 0.35:
        factor(1, "L = %s·D: в 5D слои идут вдоль всей рабочей поверхности" % _num(ratio, 2))
    elif ratio < 0.15:
        factor(-2, "тонкий диск (L = %s·D): плоские слои для него естественны" % _num(ratio, 2))
    else:
        factor(0, "диск (L = %s·D): в 3D печатается плашмя без проблем" % _num(ratio, 2))
    if diameter > limits["diameter_5d_mm"]:
        blockers.append("⌀%s мм больше 5D-области ⌀%s мм" % (_num(diameter), _num(limits["diameter_5d_mm"])))
    if length > limits["length_5d_mm"]:
        blockers.append("длина %s мм больше 5D-области %s мм" % (_num(length), _num(limits["length_5d_mm"])))
    if density:
        mass = analysis["volume_mm3"] / 1000.0 * float(density)
        if mass > limits["mass_5d_g"]:
            blockers.append("масса ≈%s г больше %s г для 5D" % (_num(mass, 0), _num(limits["mass_5d_g"], 0)))
    if blockers:
        return OrderedDict([("verdict", "3d"), ("mode", "classic"), ("blockers", blockers), ("factors", factors)])

    base, base_reason = base_diameter(revolution)
    core_radius = base / 2.0 + overlap / 2.0
    triangles, normals, areas = analysis["triangles"], analysis["normals"], analysis["areas"]
    outside = geometry.volume_outside_radius(triangles, normals, areas, revolution["axis"], revolution["center"], core_radius)
    outside_share = 100.0 * outside / max(analysis["volume_mm3"], 1e-9)
    if outside_share < 50:
        factor(-2, "только %d%% объёма лежит дальше базы ⌀%s — остальное печатается 3D-ядром" % (round(outside_share), _num(base)))

    best_3d = stable_3d(analysis)
    overhang_3d = best_3d["overhang_percent"]
    trapped = geometry.trapped_overhang_share(triangles, normals, areas, best_3d["down"], revolution["axis"])
    overhang_5d = geometry.radial_overhang_share(triangles, normals, areas, revolution["axis"], revolution["center"], core_radius)
    if trapped >= 1.0:
        factor(2, "в 3D поддержки встанут на саму деталь (каналы, промежутки между дисками) на %s%% поверхности — "
                  "их трудно удалить; в 5D эти торцевые грани идут как стенки, без поддержек" % _num(trapped))
    if overhang_5d > overhang_3d + 15:
        factor(-2, "в 5D к оси смотрит %s%% поверхности против %s%% нависаний в 3D (сильный загиб лопаток, гофры)"
               % (_num(overhang_5d), _num(overhang_3d)))
    elif overhang_5d > overhang_3d + 5:
        factor(-1, "в 5D к оси смотрит %s%% поверхности против %s%% нависаний в 3D" % (_num(overhang_5d), _num(overhang_3d)))

    if revolution["through_bore"] and revolution["bore_diameter"] >= MIN_BASE:
        factor(1, "сквозное отверстие ⌀%s мм — деталь садится базой на ось" % _num(revolution["bore_diameter"], 1))

    teeth = revolution.get("teeth")
    load_key = str(load or "").strip().lower()
    if teeth:
        # Момент на шестерне передают зубья: «кручение» из ТЗ для неё — тот же изгиб зуба, второй раз не считаем.
        factor(-2, "зубчатый венец (≈%d зубьев): момент передаётся изгибом зуба у корня — радиально, поперёк "
                   "слоёв 5D; плоские слои «лёжа» держат его лучше" % teeth["count"])
        if load_key in ("torsion", "bending"):
            load_key = ""
    if load_key in LOADS:
        label, points, why = LOADS[load_key]
        factor(points, "нагрузка — %s: %s" % (label, why))

    score = sum(f["points"] for f in factors)
    if score >= 3:
        verdict = "5d"
    elif score <= 0:
        verdict = "3d"
    else:
        verdict = "either"

    result = OrderedDict([
        ("verdict", verdict),
        ("mode", "cylindrical_full" if verdict == "5d" else "classic" if verdict == "3d" else None),
        ("score", score),
        ("factors", factors),
    ])
    if verdict == "either":
        result["decide"] = ("баллов мало для однозначного выбора — решает нагрузка: кручение, давление, качение → 5D; "
                            "изгиб зубьев и рычагов → 3D" if load_key not in LOADS else
                            "оба режима допустимы: 5D — ради поверхности и поддержек, 3D — быстрее нарезка и печать")
    if verdict != "3d":
        rpm = speed / (2.0 * math.pi * max(base / 2.0, 1e-6)) * 60.0
        result["base_diameter_mm"] = base
        result["base_reason"] = base_reason
        result["volume_in_5d_percent"] = round(outside_share, 1)
        result["overhang_5d_percent"] = round(overhang_5d, 1)
        result["c_axis_rpm_at_base"] = round(rpm)
        result["evidence"] = knowledge.FIVE_AXIS_EVIDENCE
        if analysis["triangle_count"] > DENSE_MESH:
            result["slicing_note"] = ("плотная сетка (%d треугольников): 5D-нарезка может идти десятки минут — "
                                      "упростите сетку или начните с быстрой 3D-нарезки для оценки" % analysis["triangle_count"])
    return result
