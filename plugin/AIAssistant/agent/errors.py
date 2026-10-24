# Понятные тексты ошибок для пользователя. Внутренние детали (исходный текст, traceback) уходят в журнал,
# а в чат — заголовок простыми словами, совет и действие для кнопки на карточке ошибки.

# kind: (заголовок, что сделать, действие кнопки: settings | retry | new_chat | continue | report)
KINDS = {
    "config": ("Не настроено подключение к нейросети",
               "Откройте «Настройки» и укажите URL API и ключ, выданные организаторами.", "settings"),
    "auth": ("Неверный ключ API",
             "Нейросеть не приняла ключ. Вставьте ключ заново в «Настройках» и нажмите «Проверить подключение».",
             "settings"),
    "not_found": ("Неверный адрес API или модель",
                  "Проверьте в «Настройках» URL API (обычно оканчивается на /v1) и название модели.", "settings"),
    "rate_limit": ("Слишком много запросов к нейросети",
                   "Сервис временно ограничил запросы. Подождите минуту и нажмите «Повторить».", "retry"),
    "unavailable": ("Нейросеть временно недоступна",
                    "Мы уже повторили запрос несколько раз. Проверьте интернет и нажмите «Повторить» чуть позже.",
                    "retry"),
    "ssl": ("Не удалось проверить сертификат сервера",
            "Проверьте URL API. Если это внутренний сервер организаторов, отключите проверку SSL в «Настройках».",
            "settings"),
    "too_long": ("Диалог стал слишком длинным",
                 "Нейросеть не может принять такую длинную историю. Начните новый чат и повторите задачу.",
                 "new_chat"),
    "bad_response": ("Нейросеть вернула ошибку",
                     "Нажмите «Повторить». Если ошибка повторяется, отправьте отчёт для поддержки.", "retry"),
    "steps": ("Агент не успел закончить",
              "Задача оказалась длиннее лимита шагов. Напишите «продолжай», и агент доделает её.", "continue"),
    "loop": ("Агент остановлен: он повторял одни и те же действия",
             "Проверьте, что модель прикреплена к сообщению, или уточните задачу.", "report"),
    "slicer": ("Слайсер не смог выполнить действие",
               "Проверьте сцену и настройки. Для 5D нужна активированная лицензия STE Slicer.", "report"),
    "internal": ("Внутренняя ошибка плагина",
                 "Нажмите «Отчёт для поддержки» и отправьте его разработчикам.", "report"),
}


def describe(kind):
    title, hint, action = KINDS.get(kind) or KINDS["internal"]
    return {"kind": kind if kind in KINDS else "internal", "title": title, "hint": hint, "action": action}


def classify(error):
    """Определяет класс ошибки по исключению LLMError/прочему или по тексту."""
    status = getattr(error, "status", None)
    text = str(error).lower()
    is_llm = type(error).__name__ == "LLMError"
    if "не задан url" in text or "не задана модель" in text or ("ключ" in text and "не задан" in text):
        return "config"
    if status in (401, 403) or "http 401" in text or "http 403" in text:
        return "auth"
    if status == 404 or "http 404" in text:
        return "not_found"
    if status == 429 or "http 429" in text:
        return "rate_limit"
    if "certificate" in text or "ssl" in text:
        return "ssl"
    if any(word in text for word in ("context length", "context_length", "too long", "maximum context",
                                     "token limit", "too many tokens")):
        return "too_long"
    if (status is not None and status >= 500) or getattr(error, "retryable", False) \
            or any(word in text for word in ("timed out", "timeout", "не удалось подключиться", "оборвалось",
                                             "ошибка соединения")):
        return "unavailable"
    if is_llm:
        return "bad_response"
    if type(error).__name__ in ("MainThreadTimeout",) or "slic" in text or "backend" in text:
        return "slicer"
    return "internal"
