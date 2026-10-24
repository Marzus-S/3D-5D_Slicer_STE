# Справочник материалов «как напечатано»: свойства печатных образцов, а не литьевые даташиты.
# У каждого числа — метка источника, чтобы чат не выдавал оценку за измерение.
# Семейство материала определяется по id из list_materials (enduse_pa6 → pa, enduse_tpu_d70 → flex).

from collections import OrderedDict

SOURCES = OrderedDict([
    ("printed", ("печатные образцы Stereotech",
                 "https://github.com/stereotech/User-Manuals/blob/master/ru/printers/hybrid530/5-materials.md")),
    ("drying", ("режимы сушки QIDI", "https://qidi3d.com/ru/blogs/news/mastering-filament-drying-techniques")),
    ("ornl", ("ORNL: по Z прочность FFF на 50–75 % ниже",
              "https://impact.ornl.gov/en/publications/z-pinning-approach-for-improving-interlayer-strength-of-3d-printe/")),
    ("estimate", ("оценка: типовые рекомендации производителей пластиков", None)),
])

# Свойства из руководства Hybrid 530 («Материалы»), образцы напечатаны на принтерах Stereotech.
PROPERTIES = OrderedDict([
    ("tensile_xy", ("Прочность при растяжении вдоль слоёв", "МПа")),
    ("tensile_z", ("Прочность при растяжении поперёк слоёв", "МПа")),
    ("flexural", ("Прочность при изгибе", "МПа")),
    ("compressive_yield", ("Предел текучести при сжатии", "МПа")),
    ("impact", ("Ударная вязкость по Шарпи", "кДж/м²")),
])

# Порядок важен: petg раньше pet, pa — по целому слову id.
FAMILIES = OrderedDict([
    ("abs", {"label": "ABS", "marks": ("abs",), "service": (-40, 80), "hygroscopic": False,
             "drying": (60, 80, 2, 4, "estimate"),
             "printed": {"tensile_xy": 29.6, "tensile_z": 19.7, "flexural": 65.4, "compressive_yield": 49.3,
                         "impact": 180.14}}),
    ("asa", {"label": "ASA", "marks": ("asa",), "service": (-40, 90), "hygroscopic": False,
             "drying": (60, 80, 2, 4, "estimate"),
             "printed": {"tensile_xy": 35.8, "tensile_z": 22.5, "flexural": 69.5, "compressive_yield": 56.5,
                         "impact": 197.06}}),
    ("pla", {"label": "PLA", "marks": ("pla",), "service": (-20, 40), "hygroscopic": False,
             "drying": (45, 50, 4, 6, "estimate"),
             "printed": {"tensile_xy": 34.8, "tensile_z": 31.2, "flexural": 94.2, "compressive_yield": 77.4,
                         "impact": 5.62}}),
    ("petg", {"label": "PETG", "marks": ("petg", "pet"), "service": (-40, 70), "hygroscopic": True,
              "drying": (60, 65, 4, 6, "estimate"),
              "printed": {"tensile_xy": 36.5, "tensile_z": 33.6, "flexural": 76.1, "compressive_yield": 51.7,
                          "impact": 4.17}}),
    ("pa", {"label": "Полиамид (PA)", "marks": ("pa", "pa6", "pa12", "nylon"), "service": (-60, 80),
            "hygroscopic": True, "drying": (70, 80, 8, 12, "drying"),
            "printed": {"tensile_xy": 50, "tensile_z": 45, "flexural": 60, "compressive_yield": 60, "impact": 85}}),
    ("flex", {"label": "Гибкий (TPU, FLEX)", "marks": ("tpu", "tpe", "tpv", "sebs", "flex"), "service": (-40, 100),
              "hygroscopic": True, "drying": (50, 55, 4, 6, "estimate"),
              "printed": {"tensile_xy": 17.5, "flexural": 5.3, "compressive_yield": 7.6}}),
    ("pc", {"label": "Поликарбонат (PC)", "marks": ("pc",), "service": None, "hygroscopic": True,
            "drying": (80, 90, 4, 6, "estimate"), "printed": {}, "printblock": 2}),
    ("pp", {"label": "Полипропилен (PP)", "marks": ("pp",), "service": None, "hygroscopic": False,
            "drying": None, "printed": {}}),
])
FILLED_MARKS = ("cf", "gf", "c5", "c10", "g10", "g20", "g30", "g40")
Z_LOSS_RANGE = (0.25, 0.5)
RULE = ("Литьевые даташиты описывают литьё. Для печатной детали бери значения «как напечатано»; если их нет — "
        "считай по Z не больше 0,25–0,5 от литьевой прочности и учитывай влагу.")


def family_of(material_id):
    """Ключ семейства по id или названию материала, None — не распознан."""
    text = str(material_id or "").lower().replace("-", "_").replace(" ", "_")
    words = [w for w in text.split("_") if w]
    if text in FAMILIES:
        return text
    for key, family in FAMILIES.items():
        for mark in family["marks"]:
            if mark in words or (len(mark) >= 3 and any(w.startswith(mark) for w in words)):
                return key
    return None


def _source(key):
    title, url = SOURCES[key]
    return OrderedDict([("key", key), ("title", title), ("url", url)])


def info(material_id):
    """Свойства материала «как напечатано» с источником у каждого числа."""
    key = family_of(material_id)
    if key is None:
        raise ValueError("Нет данных для «%s». Есть семейства: %s" % (
            material_id, ", ".join(f["label"] for f in FAMILIES.values())))
    family = FAMILIES[key]
    words = str(material_id).lower().replace("-", "_").split("_")
    filled = any(w in FILLED_MARKS for w in words)
    result = OrderedDict([("material", material_id), ("family", key), ("label", family["label"])])

    props = []
    for prop, value in family["printed"].items():
        title, unit = PROPERTIES[prop]
        props.append(OrderedDict([("key", prop), ("label", title), ("value", value), ("unit", unit),
                                  ("source", "printed")]))
    result["properties"] = props

    printed = family["printed"]
    if printed.get("tensile_xy") and printed.get("tensile_z"):
        ratio = round(printed["tensile_z"] / float(printed["tensile_xy"]), 2)
        result["z_ratio"] = OrderedDict([("value", ratio), ("source", "printed"),
                                         ("note", "поперёк слоёв — %d %% прочности вдоль слоёв" % round(ratio * 100))])
    else:
        result["z_ratio"] = OrderedDict([("value", list(Z_LOSS_RANGE)), ("source", "ornl"),
                                         ("note", "измерений нет — закладывай 25–50 % по Z")])

    if family["service"]:
        result["service_temp_c"] = OrderedDict([("value", list(family["service"])), ("source", "printed")])
    if family["drying"]:
        low, high, hours_low, hours_high, source = family["drying"]
        result["drying"] = OrderedDict([("temp_c", [low, high]), ("hours", [hours_low, hours_high]),
                                        ("source", source)])
    result["hygroscopic"] = family["hygroscopic"]
    if filled or family.get("printblock") == 2:
        result["printblock"] = OrderedDict([("value", 2), ("source", "printed"),
                                            ("note", "абразивный или высокотемпературный — второй принтблок "
                                                     "с цельнометаллической трубкой")])
    if filled:
        result["filled_note"] = ("Наполненный волокном: значения выше для ненаполненной основы, жёсткость будет "
                                 "выше, ударная вязкость ниже — уточните по паспорту партии.")
    used = []
    for value in [p["source"] for p in props] + [result["z_ratio"]["source"]] + [
            result[k]["source"] for k in ("service_temp_c", "drying", "printblock") if k in result]:
        if value not in used:
            used.append(value)
    result["sources"] = [_source(k) for k in SOURCES if k in used]
    result["rule"] = RULE
    return result


def drying_hint(material_id):
    """Одна фраза о сушке для гигроскопичного материала, иначе None."""
    key = family_of(material_id)
    if key is None or not FAMILIES[key]["hygroscopic"] or not FAMILIES[key]["drying"]:
        return None
    low, high, hours_low, hours_high, _ = FAMILIES[key]["drying"]
    return "%s набирает влагу: перед печатью сушите при %d–%d °C %d–%d ч" % (
        FAMILIES[key]["label"], low, high, hours_low, hours_high)
