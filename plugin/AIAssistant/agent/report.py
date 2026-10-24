# HTML-отчёт об ошибке для техподдержки: понятное описание сверху, поле «Что вы делали?»,
# технические подробности под спойлером и кнопки «Скопировать» / «Скачать». Страница самодостаточная:
# её можно сохранить и отправить почтой. Сама форма ничего никуда не отправляет.

import html
import json

from .errors import describe

_LABELS = [
    ("code", "Код ошибки"),
    ("time", "Дата и время"),
    ("user", "Пользователь"),
    ("version", "Версия плагина"),
    ("printer", "Принтер"),
    ("printing_mode", "Режим печати"),
    ("llm_model", "Модель LLM"),
    ("session", "Чат"),
]

_EVENT_TITLES = {
    "user": "Пользователь",
    "assistant": "Ответ модели",
    "assistant_note": "Пояснение модели",
    "tool_call": "Вызов инструмента",
    "tool_result": "Результат инструмента",
    "slice_result": "Итог слайсинга",
    "file_saved": "Файл сохранён",
    "status": "Статус",
    "run_end": "Конец запроса",
}


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def _human_time(value):
    """2026-10-24T12:03:19.540+0300 → 24.10.2026 12:03:19 (UTC+03:00)."""
    value = value or ""
    if len(value) < 19:
        return value
    zone = value[23:] if len(value) > 23 else ""
    zone = " (UTC%s:%s)" % (zone[:3], zone[3:]) if len(zone) == 5 else ""
    return "%s.%s.%s %s%s" % (value[8:10], value[5:7], value[0:4], value[11:19], zone)


def _event_line(entry):
    data = entry.get("data") or {}
    kind = entry.get("event")
    if kind in ("user", "assistant", "assistant_note", "status"):
        text = data.get("text") or ""
        if data.get("attachments"):
            text += "  [файлы: %s]" % ", ".join(data["attachments"])
    elif kind == "tool_call":
        text = "%s %s" % (data.get("name"), json.dumps(data.get("args") or {}, ensure_ascii=False))
    elif kind == "tool_result":
        text = "%s → %s" % (data.get("name"), "ok" if data.get("ok") else "ошибка: " + str(data.get("result") or ""))
    else:
        text = json.dumps(data, ensure_ascii=False)
    if len(text) > 400:
        text = text[:400] + "…"
    return entry.get("time", "")[11:19], _EVENT_TITLES.get(kind, kind), text


def render(record, events, context):
    """record — запись из errors/, events — последние события чата, context — состояние слайсера."""
    error = record.get("error") or {}
    info = describe(error.get("kind"))
    fields = {
        "code": error.get("code"),
        "time": _human_time(record.get("time")),
        "user": record.get("user"),
        "version": record.get("version"),
        "session": record.get("session"),
    }
    fields.update(context or {})

    rows = "".join(
        '<label><span>%s</span><input readonly name="%s" value="%s"></label>' % (_e(label), _e(key), _e(fields.get(key) or "—"))
        for key, label in _LABELS)
    event_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(_e(part) for part in _event_line(entry))
        for entry in events) or '<tr><td colspan="3">Событий не найдено</td></tr>'
    trace = error.get("traceback") or ""
    internal = dict(record)
    internal["error"] = dict((k, v) for k, v in error.items() if k != "traceback")
    internal = json.dumps(internal, ensure_ascii=False, indent=2)

    return _TEMPLATE % {
        "code": _e(error.get("code")),
        "title": _e(info["title"]),
        "hint": _e(info["hint"]),
        "message": _e(error.get("message")),
        "rows": rows,
        "events": event_rows,
        "internal": _e(internal + ("\n\nTraceback:\n" + trace if trace else "")),
    }


_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Отчёт об ошибке %(code)s</title>
<style>
  :root { --bg: #f2f5f4; --panel: #fff; --text: #1d2a30; --muted: #61717a; --line: #dbe3e1;
          --err: #c0392b; --err-soft: #fdeceb; --accent: #9cc23a; --code: #f1f4f3; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0f1318; --panel: #181e25; --text: #e4ebef; --muted: #8d9ba5; --line: #2a333c;
            --err: #ff7b6e; --err-soft: #3a1d1b; --accent: #b4d455; --code: #11161c; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }
  main { max-width: 820px; margin: 0 auto; padding: 24px 16px 48px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; }
  .alert { border-color: var(--err); background: var(--err-soft); }
  .alert h1 { color: var(--err); font-size: 20px; margin: 0 0 6px; }
  .alert p { margin: 0; }
  .code { font: 13px Consolas, monospace; color: var(--muted); margin-top: 8px; }
  h2 { font-size: 16px; margin: 0 0 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px 14px; }
  label span { display: block; font-size: 12.5px; color: var(--muted); margin-bottom: 2px; }
  input, textarea { width: 100%%; font: inherit; color: inherit; background: var(--code); border: 1px solid var(--line);
                    border-radius: 8px; padding: 7px 9px; }
  textarea { min-height: 110px; resize: vertical; background: var(--panel); }
  .buttons { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
  button { font: inherit; border: none; border-radius: 999px; padding: 8px 18px; cursor: pointer;
           background: var(--accent); color: #13220a; font-weight: 600; }
  button.secondary { background: transparent; color: var(--text); border: 1px solid var(--line); font-weight: 500; }
  .done { color: var(--muted); font-size: 13px; align-self: center; }
  details summary { cursor: pointer; font-weight: 600; }
  pre { background: var(--code); border-radius: 8px; padding: 12px; overflow-x: auto; font: 12.5px Consolas, monospace;
        white-space: pre-wrap; word-break: break-word; }
  table { width: 100%%; border-collapse: collapse; font-size: 13px; margin-top: 10px; table-layout: fixed; }
  td { border-top: 1px solid var(--line); padding: 5px 6px; vertical-align: top; overflow-wrap: anywhere; }
  td:first-child { width: 70px; }
  td:nth-child(2) { width: 160px; }
  td:first-child { white-space: nowrap; color: var(--muted); }
  td:nth-child(2) { white-space: nowrap; }
</style>
</head>
<body>
<main>
  <div class="card alert">
    <h1>%(title)s</h1>
    <p>%(hint)s</p>
    <div class="code">Код ошибки: %(code)s</div>
  </div>

  <form class="card" id="report" onsubmit="return false">
    <h2>Отчёт для поддержки</h2>
    <div class="grid">%(rows)s</div>
    <label style="display:block;margin-top:14px">
      <span>Что вы делали, когда появилась ошибка? Что ожидали увидеть?</span>
      <textarea name="comment" placeholder="Например: прикрепила Колесо.stl, попросила подобрать режим 5D, после этого появилась ошибка"></textarea>
    </label>
    <div class="buttons">
      <button type="button" id="copy">Скопировать отчёт</button>
      <button type="button" class="secondary" id="download">Скачать .txt</button>
      <span class="done" id="done"></span>
    </div>
  </form>

  <div class="card">
    <details>
      <summary>Технические подробности</summary>
      <p><b>Исходное сообщение:</b> %(message)s</p>
      <h2 style="margin-top:14px">Последние события чата</h2>
      <table>%(events)s</table>
      <h2 style="margin-top:14px">Внутренний лог ошибки</h2>
      <pre id="internal">%(internal)s</pre>
    </details>
  </div>
</main>
<script>
(function () {
  function reportText() {
    var form = document.getElementById("report");
    var lines = ["Отчёт об ошибке AI Assistant для STE Slicer", ""];
    Array.prototype.forEach.call(form.querySelectorAll("label"), function (label) {
      var field = label.querySelector("input, textarea");
      lines.push(label.querySelector("span").textContent + ": " + (field.value || "—"));
    });
    lines.push("", "Внутренний лог:", document.getElementById("internal").textContent);
    return lines.join("\\n");
  }
  function done(text) { document.getElementById("done").textContent = text; }
  document.getElementById("copy").onclick = function () {
    var text = reportText();
    var fallback = function () {
      var area = document.createElement("textarea");
      area.value = text; document.body.appendChild(area); area.select();
      try { document.execCommand("copy"); done("Скопировано"); } catch (e) { done("Не удалось скопировать"); }
      document.body.removeChild(area);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { done("Скопировано"); }, fallback);
    } else { fallback(); }
  };
  document.getElementById("download").onclick = function () {
    var link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([reportText()], { type: "text/plain;charset=utf-8" }));
    link.download = "ai-assistant-%(code)s.txt";
    document.body.appendChild(link); link.click(); document.body.removeChild(link);
    done("Файл сохранён");
  };
})();
</script>
</body>
</html>
"""
