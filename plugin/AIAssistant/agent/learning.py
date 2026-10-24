# Обучение: банк вопросов для квизов, правила разбора настроек ученика и его прогресс.
# Без зависимостей от слайсера — проверяется отдельно от UM. Правила согласованы с knowledge.SYSTEM_PROMPT.

import json
import os
import random
import threading
from collections import OrderedDict

# OrderedDict: плагин работает на Python 3.5, где обычный dict порядок не хранит.
TOPICS = OrderedDict([
    ("mode", "Режим 3D / 5D"),
    ("material", "Материалы"),
    ("infill", "Заполнение"),
    ("walls", "Стенки и сплошные слои"),
    ("supports", "Поддержки и ориентация"),
    ("adhesion", "Прилипание к столу"),
    ("quality", "Профиль качества"),
    ("flow", "Скорость и расход"),
])
TOPIC_ALIASES = {
    "режим": "mode", "3d": "mode", "5d": "mode", "материал": "material", "материалы": "material",
    "заполнение": "infill", "стенки": "walls", "поддержки": "supports", "ориентация": "supports",
    "адгезия": "adhesion", "прилипание": "adhesion", "профиль": "quality", "качество": "quality",
    "скорость": "flow", "расход": "flow",
}

# Первый вариант — верный; при показе варианты перемешиваются.
QUESTIONS = [
    ("mode_disc", "mode", "Шестерня диаметром 60 мм и толщиной 12 мм из жёсткого пластика. Какой режим?",
     ["3D", "5D", "5D Spiral"],
     "Длина вдоль оси 12 / 60 = 0,2 диаметра, меньше 0,35 — это диск. Диски печатают в 3D."),
    ("mode_roller", "mode", "Ролик конвейера: диаметр 40 мм, длина 60 мм, жёсткий материал. Какой режим?",
     ["5D", "3D", "5D Spiral"],
     "Длина больше 0,35 диаметра и есть ось вращения — 5D: слои ложатся вокруг оси, поверхность гладкая, "
     "деталь прочнее на кручение."),
    ("mode_flexible", "mode", "Гофрированный пыльник из гибкого TPU. Какой режим?",
     ["3D", "5D", "5D Spiral"],
     "Гибкие материалы (пыльники, уплотнения) печатают в 3D."),
    ("mode_shell", "mode", "Тонкостенная оболочка вращения, стенка 1 мм. Какой режим подходит лучше всего?",
     ["5D Spiral", "3D", "5D"],
     "5D Spiral рассчитан на тонкостенные оболочки вращения: стенка идёт одной спиралью вокруг оси."),
    ("mode_base", "mode", "Деталь для 5D со сквозным отверстием 20 мм. Каким должен быть cylindrical_mode_base_diameter?",
     ["Не больше 20 мм", "Больше 20 мм", "Не важно"],
     "Цилиндрическая база проходит внутри отверстия, поэтому её диаметр не больше диаметра отверстия."),
    ("mode_axis", "mode", "Как поставить деталь для печати в 5D?",
     ["Ось вращения вертикально, по центру стола", "Самой большой гранью на стол", "Под углом 45°"],
     "В 5D деталь печатается вокруг базы в центре стола, поэтому её ось — вертикально через центр (axis_vertical)."),
    ("mat_glycol", "material", "Деталь работает в горячем тосоле (гликоле) и щелочной среде. Какой материал?",
     ["Fiberpart PP G30", "Поликарбонат (PC)", "PLA"],
     "PP G30 стоек к гидролизу и горячему тосолу. Поликарбонат в горячей щёлочи гидролизуется."),
    ("mat_paper", "material", "Ролик катится по белой бумаге и не должен оставлять следов. Чего избегать?",
     ["Материалов с углеволокном", "Твёрдого TPU", "Любого пластика"],
     "Углеволокно (C5 и подобные) пачкает бумагу. Для фрикционных роликов подходит твёрдый TPU (Enduse TPU D70)."),
    ("mat_friction", "material", "Втулка трётся по ответной детали без смазки. Почему лучше PA6 без стекловолокна?",
     ["Стекловолокно стачивает ответную деталь", "Стекловолокно делает деталь гибкой", "PA6 печатается быстрее"],
     "Наполнитель абразивен: пара трения быстро изнашивается. Чистый PA6 хорошо работает на трение."),
    ("mat_flexible", "material", "Нужна гибкая деталь, например уплотнение. Какую группу материалов взять?",
     ["TPU / TPE / TPV / SEBS", "PETG", "PA с углеволокном"],
     "TPU, TPE, TPV и SEBS — эластичные материалы, остальные жёсткие."),
    ("mat_glass", "material", "Чем материалы со стекловолокном (G30, GF) отличаются от ненаполненных?",
     ["Жёстче и теплостойче, но абразивны", "Мягче и гибче", "Ничем"],
     "Стекловолокно повышает жёсткость и теплостойкость, но стачивает ответные детали."),
    ("infill_decor", "infill", "Декоративная фигурка без нагрузки. Какое заполнение разумно?",
     ["10–20 %", "50 %", "100 %"],
     "Декору прочность не нужна: 10–20 % экономит время и материал."),
    ("infill_functional", "infill", "Функциональная деталь со средней нагрузкой. Какое заполнение?",
     ["30–50 %", "5 %", "Всегда 100 %"],
     "Функциональным деталям обычно хватает 30–50 %."),
    ("infill_loaded", "infill", "Нагруженная зона или герметичная деталь. Какое заполнение?",
     ["60–100 %", "10–20 %", "30 %"],
     "Под нагрузкой и для герметичности нужен плотный материал: 60–100 %."),
    ("infill_effect", "infill", "Что произойдёт, если поднять заполнение с 20 до 80 %?",
     ["Деталь станет прочнее, но печать дольше и тяжелее", "Печать пойдёт быстрее", "Ничего не изменится"],
     "Больше пластика внутри — выше прочность, но растут время печати и расход материала."),
    ("infill_pattern", "infill", "Какого узора заполнения нет в STE Slicer?",
     ["gyroid", "cubic", "triangles"],
     "В слайсере есть cubic и triangles, gyroid нет."),
    ("walls_gear", "walls", "Нагруженная шестерня. Сколько стенок (wall_line_count)?",
     ["4–6", "1", "2"],
     "Нагрузка идёт по контуру зуба, поэтому нагруженным деталям ставят 4–6 стенок."),
    ("walls_why", "walls", "Почему для прочности зубьев стенки важнее заполнения?",
     ["Нагрузка идёт по внешнему контуру детали", "Стенки печатаются быстрее", "Они не важнее"],
     "Изгиб и срез зуба воспринимает внешний контур — его толщину задают стенки."),
    ("walls_skin", "walls", "Сколько сплошных слоёв сверху и снизу (top_layers / bottom_layers) обычно ставят?",
     ["4–8", "1–2", "20"],
     "4–8 слоёв закрывают заполнение и держат форму поверхности; меньше — просвечивает узор."),
    ("supports_off", "supports", "Пользователь просит «без поддержек». Что нужно сделать?",
     ["support_enable = false и ориентация на минимум нависаний", "Оставить поддержки", "Уменьшить заполнение"],
     "Без поддержек нависания должны быть минимальными — это решает ориентация (min_supports)."),
    ("supports_angle", "supports", "Какое нависание уже обычно требует поддержки?",
     ["Круче 45° от вертикали", "Любое больше 10°", "Только горизонтальное, 90°"],
     "До ~45° слой опирается на предыдущий, круче — провисает без поддержки."),
    ("adhesion_brim", "adhesion", "Малая площадь опоры и усадочный материал. Что поможет детали не оторваться от стола?",
     ["brim (кайма)", "Без адгезии", "Повысить скорость"],
     "Кайма увеличивает площадь контакта первого слоя и держит углы от отрыва."),
    ("adhesion_5d", "adhesion", "Какой тип прилипания по умолчанию ставят в 5D?",
     ["raft (подложка)", "brim", "skirt"],
     "В 5D по умолчанию raft: подложка на цилиндрической базе."),
    ("quality_fine", "quality", "Точная деталь с мелкими элементами (зубья, резьба). Какой профиль?",
     ["Мельче: Fine / Extra Fine", "Крупнее: Draft", "Любой"],
     "Тонкий слой точнее передаёт мелкие элементы и поверхность."),
    ("quality_time", "quality", "Как профиль качества влияет на время печати?",
     ["Тоньше слой — дольше печать", "Тоньше слой — быстрее", "Не влияет"],
     "Слоёв становится больше, поэтому печать идёт дольше."),
    ("mode_gear_5d", "mode", "Почему для изгиба зуба шестерни 5D может оказаться хуже плоской печати?",
     ["Напряжение у корня зуба радиальное — поперёк цилиндрических слоёв", "5D печатает медленнее",
      "В 5D нельзя PA6"],
     "Слабое место печатной детали — граница слоёв. У цилиндрических слоёв она поперёк радиуса, а изгиб зуба "
     "нагружает именно радиальное направление."),
    ("mode_evidence", "mode", "Какой выигрыш прочности 5D подтверждают независимые испытания?",
     ["+40–85 %, когда нагрузка идёт вдоль слоёв", "Всегда ×3–4", "Выигрыша нет"],
     "Независимые работы дают +40–85 % при совпадении нагрузки со слоями; ×3–4 — заявление производителя "
     "без опубликованной методики."),
    ("mat_nylon_wet", "material", "Нейлон (PA) пролежал сутки на открытом воздухе. Что сделать перед печатью?",
     ["Просушить: около 70–80 °C 8–12 ч", "Ничего, PA не боится влаги", "Поднять скорость"],
     "PA быстро набирает влагу: появляются нити и пузыри, падают прочность и удлинение. Перед печатью его сушат."),
    ("mat_datasheet", "material", "Можно ли брать прочность из литьевого даташита для печатной детали?",
     ["Нет: печатная деталь слабее, по Z — на 50–75 %", "Да, цифры те же", "Только для PLA"],
     "Даташит описывает литьё. Печатная деталь слабее, особенно поперёк слоёв, — закладывайте запас."),
    ("walls_z", "walls", "Где печатная FDM-деталь слабее всего?",
     ["Поперёк слоёв (по Z)", "Вдоль линий стенки", "Везде одинакова"],
     "Межслойное сцепление слабее самого пластика: по Z прочность на 50–75 % ниже. Ориентируйте деталь так, "
     "чтобы нагрузка шла вдоль слоёв."),
    ("quality_hole", "quality", "Отверстие ⌀8 мм напечаталось ⌀7,8 мм. Это брак?",
     ["Нет, отверстия в FDM обычно выходят меньше номинала", "Да, сломан принтер", "Это из-за заполнения"],
     "Отверстия в FDM сжимаются на 0,1–0,4 мм. Для точных посадок делают поправку в модели или развёртку."),
    ("quality_it12", "quality", "Можно ли напечатать на Hybrid 530 посадку 20H7 под подшипник?",
     ["Нет: точность печати около IT12, нужна развёртка", "Да, с профилем Extra Fine", "Да, при 100 % заполнения"],
     "Hybrid 530 даёт точность около IT12, а H7 намного точнее. Отверстие печатают с припуском и разворачивают."),
    ("adhesion_foot", "adhesion", "Нижние слои детали расплылись наружу («слоновья нога»). Какая настройка помогает?",
     ["Отрицательный xy_offset_layer_0", "Больше заполнения", "Выше скорость первого слоя"],
     "Первый слой прижимается к столу и расплывается. Отрицательная горизонтальная компенсация первого слоя "
     "сужает его контур."),
    ("flow_define", "flow", "Что такое объёмный расход Q?",
     ["Скорость × высота слоя × ширина линии, мм³/с", "Масса детали, г", "Скорость обдува"],
     "Q показывает, сколько пластика в секунду должно расплавиться в сопле."),
    ("flow_high", "flow", "Расход Q выше ориентира для материала. Что сделать?",
     ["Снизить speed_print", "Поднять скорость", "Отключить обдув"],
     "Сопло не успевает расплавить пластик: недоэкструзия и слабое спекание слоёв. Помогает снижение скорости."),
    ("flow_flexible", "flow", "Почему гибкие материалы печатают медленнее?",
     ["Мягкий пруток хуже проталкивается подачей", "Они быстро остывают", "Так ровнее цвет"],
     "Мягкий пруток сжимается в подаче, поэтому для TPU ориентир расхода ниже — около 5 мм³/с."),
]
_BY_ID = {q[0]: q for q in QUESTIONS}

# Рекомендуемые диапазоны по назначению детали (knowledge.SYSTEM_PROMPT, раздел «Настройки»).
PURPOSES = OrderedDict([
    ("decor", "декор"),
    ("functional", "функциональная"),
    ("loaded", "нагруженная"),
    ("sealed", "герметичная"),
    ("flexible", "гибкая"),
])
PURPOSES_OF = {"decor": "декоративной", "functional": "функциональной", "loaded": "нагруженной",
               "sealed": "герметичной", "flexible": "гибкой"}
RANGES = OrderedDict([
    ("infill_sparse_density", {"decor": (10, 20), "functional": (30, 50), "loaded": (60, 100), "sealed": (60, 100),
                               "flexible": (10, 40)}),
    ("wall_line_count", {"decor": (2, 3), "functional": (3, 5), "loaded": (4, 6), "sealed": (4, 6),
                         "flexible": (2, 4)}),
    ("top_layers", {p: (4, 8) for p in PURPOSES}),
    ("bottom_layers", {p: (4, 8) for p in PURPOSES}),
])
RANGE_TOPICS = {"infill_sparse_density": "infill", "wall_line_count": "walls", "top_layers": "walls",
                "bottom_layers": "walls"}
FLEXIBLE_MARKS = ("tpu", "tpe", "tpv", "sebs", "sealant")
FLEXIBLE_MAX_SPEED = 30  # мм/с


def is_flexible(material_id):
    material_id = str(material_id or "").lower()
    return any(mark in material_id for mark in FLEXIBLE_MARKS)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value):
    number = _number(value)
    if number is not None and not isinstance(value, bool) and number == int(number):
        return str(int(number))
    return str(value)


def review(data, purpose=None):
    """Разбор настроек: data — снимок из SlicerTools._review_data. Возвращает находки с советами;
    объяснять их ученику будет модель, проверка — здесь, детерминированно."""
    purpose_note = None
    if purpose not in PURPOSES:
        purpose = "flexible" if is_flexible(data.get("material_id")) else "functional"
        purpose_note = ("Назначение не указано — проверено как «%s». Передай purpose (decor, functional, loaded, "
                        "sealed, flexible), если деталь другая." % PURPOSES[purpose])
    values = data.get("values") or {}
    findings, fine = [], []

    def add(key, severity, problem, advice, topic, value=None):
        entry = values.get(key) or {}
        findings.append({"key": key, "label": entry.get("label") or key,
                         "value": entry.get("value") if value is None else value,
                         "severity": severity, "problem": problem, "advice": advice, "topic": topic,
                         "changed_by_user": key in (data.get("user_changes") or {})})

    for item in data.get("problems") or []:
        add(item["key"], "ошибка", item["error"], "Верните значение в допустимый диапазон слайсера.", None)

    for key, by_purpose in RANGES.items():
        entry = values.get(key)
        number = _number(entry.get("value")) if entry else None
        if number is None:
            continue
        low, high = by_purpose[purpose]
        if number < low:
            add(key, "предупреждение", "%s — меньше рекомендуемых %s–%s для %s детали" % (
                _fmt(entry["value"]), low, high, PURPOSES_OF[purpose]),
                "Поставьте %s–%s: иначе деталь может не выдержать нагрузку или просветит заполнение." % (low, high),
                RANGE_TOPICS[key])
        elif number > high:
            add(key, "совет", "%s — больше нужного для %s детали (%s–%s)" % (
                _fmt(entry["value"]), PURPOSES_OF[purpose], low, high),
                "Можно снизить до %s: печать быстрее и легче без потери нужной прочности." % high, RANGE_TOPICS[key])
        else:
            fine.append(key)

    mode = data.get("mode")
    adhesion = (values.get("adhesion_type") or {}).get("value")
    if mode and mode != "classic" and adhesion and adhesion != "raft":
        add("adhesion_type", "совет", "В 5D стоит «%s»" % adhesion, "В 5D по умолчанию ставят raft.", "adhesion")
    if mode and mode != "classic" and is_flexible(data.get("material_id")):
        add("printing_mode", "предупреждение", "Гибкий материал в режиме 5D",
            "Гибкие материалы печатают в 3D (classic).", "mode", value=mode)

    speed = _number((values.get("speed_print") or {}).get("value"))
    if is_flexible(data.get("material_id")) and speed and speed > FLEXIBLE_MAX_SPEED:
        add("speed_print", "предупреждение", "Скорость %s мм/с для гибкого материала" % _fmt(speed),
            "Снизьте до %d мм/с: мягкий пруток плохо проталкивается." % FLEXIBLE_MAX_SPEED, "flow")
    flow = data.get("flow") or {}
    if flow.get("warning"):
        add("speed_print", "предупреждение", "Расход %s мм³/с выше ориентира %s" % (
            flow.get("volumetric_flow_mm3_s"), flow.get("limit_mm3_s")), flow["warning"], "flow")

    if (values.get("infill_pattern") or {}).get("value") == "gyroid":
        add("infill_pattern", "ошибка", "Узор gyroid", "Выберите cubic или triangles.", "infill")

    result = {
        "purpose": purpose,
        "printing_mode": mode,
        "material": data.get("material_name") or data.get("material_id"),
        "quality": data.get("quality"),
        "findings": findings,
        "ok": fine,
        "user_changes": data.get("user_changes") or {},
        "summary": "Всё в порядке" if not findings else "Найдено замечаний: %d" % len(findings),
    }
    if purpose_note:
        result["note"] = purpose_note
    return result


class LearningTools:
    """Квизы и прогресс ученика. Прогресс — один JSON рядом с историей чатов."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._data = {"answers": {}}
        try:
            with open(path, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if isinstance(loaded, dict) and isinstance(loaded.get("answers"), dict):
                self._data = loaded
        except (OSError, ValueError):
            pass

    def _save(self):
        directory = os.path.dirname(self._path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        temporary = self._path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(self._data, stream, ensure_ascii=False)
        os.replace(temporary, self._path)

    @staticmethod
    def _topic(topic):
        if not topic:
            return None
        text = str(topic).strip().lower()
        text = TOPIC_ALIASES.get(text, text)
        return text if text in TOPICS else None

    def quiz(self, topic=None, count=2):
        """Вопросы с перемешанными вариантами, без ответов: проверяет код по кнопке в чате."""
        wanted = self._topic(topic)
        if topic and not wanted:
            raise ValueError("Неизвестная тема «%s». Темы: %s" % (topic, ", ".join(TOPICS)))
        count = max(1, min(int(count or 2), 5))
        pool = [q for q in QUESTIONS if wanted is None or q[1] == wanted]
        with self._lock:
            answers = self._data["answers"]
            # Сначала то, на что ещё не ответили верно; среди них — темы, где ошибок больше.
            weak = {t: self._accuracy(t) for t in TOPICS}
            fresh = [q for q in pool if not (answers.get(q[0]) or [0])[0]]
            ordered = sorted(fresh, key=lambda q: (weak[q[1]], random.random())) or random.sample(pool, len(pool))
        picked = ordered[:count]
        questions = []
        for qid, qtopic, text, options, _ in picked:
            shuffled = list(options)
            random.shuffle(shuffled)
            questions.append({"id": qid, "topic": qtopic, "topic_label": TOPICS[qtopic],
                              "question": text, "options": shuffled})
        return {"questions": questions,
                "note": "Вопросы уже показаны пользователю кнопками. Не повторяй их и не подсказывай ответ."}

    def check(self, qid, choice):
        question = _BY_ID.get(qid)
        if question is None:
            raise ValueError("Нет такого вопроса")
        correct = str(choice).strip() == question[3][0]
        with self._lock:
            right, total = self._data["answers"].get(qid) or [0, 0]
            self._data["answers"][qid] = [right + (1 if correct else 0), total + 1]
            self._save()
            score = self.score()
        return {"correct": correct, "answer": question[3][0], "why": question[4], "topic": question[1], "score": score}

    def _accuracy(self, topic):
        right = total = 0
        for qid, (r, t) in self._data["answers"].items():
            if qid in _BY_ID and _BY_ID[qid][1] == topic:
                right, total = right + r, total + t
        return right / total if total else 0.5

    def score(self):
        """Сколько вопросов банка ученик хоть раз решил верно и уровень по этой доле."""
        solved = sum(1 for qid, (r, _) in self._data["answers"].items() if qid in _BY_ID and r)
        share = solved / float(len(QUESTIONS))
        level = "Технолог" if share >= 0.8 else "Практик" if share >= 0.4 else "Новичок"
        return {"solved": solved, "total": len(QUESTIONS), "level": level}
