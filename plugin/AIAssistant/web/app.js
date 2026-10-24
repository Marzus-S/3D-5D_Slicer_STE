// SPA чата: история чатов, режимы «обучение / новичок / инженер / профи», быстрые команды, квизы,
// вложения, шаги агента, прогресс слайсинга, темы.
(function () {
  "use strict";

  // ---------- токен сессии (передаётся в #token=… из меню слайсера) ----------
  var hashMatch = location.hash.match(/token=([0-9a-f]+)/);
  if (hashMatch) {
    try { sessionStorage.setItem("ste-ai-token", hashMatch[1]); } catch (e) {}
    history.replaceState(null, "", location.pathname);
  }
  var token = hashMatch ? hashMatch[1] : (function () {
    try { return sessionStorage.getItem("ste-ai-token") || ""; } catch (e) { return ""; }
  })();

  // ---------- личные настройки вида (только в этом браузере) ----------
  function stored(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
  }
  function store(key, value) {
    try { localStorage.setItem(key, value); } catch (e) {}
  }

  var $ = function (id) { return document.getElementById(id); };
  var messagesEl = $("messages");
  var chatEl = $("chat");
  var inputEl = $("input");
  var attachmentsEl = $("attachments");
  var pending = [];        // вложения, ещё не отправленные
  var sessionId = "";      // открытый чат
  var lastEventId = 0;
  var busyHere = false;
  var toolCards = {};      // call_id → элемент карточки
  var quizCards = {};      // id события квиза → карточка
  var commandList = [];    // быстрые команды из /api/commands
  var progressEl = null;
  var mode = stored("ste-ai-mode", "engineer");
  var theme = stored("ste-ai-theme", "studio");

  var MODE_LABELS = { classic: "3D", cylindrical: "5D Spiral", cylindrical_full: "5D" };
  var CHAT_MODES = ["learning", "novice", "engineer", "pro"];

  var WELCOME = {
    learning: {
      badge: "Режим: обучение",
      title: "Научитесь готовить детали сами",
      text: "Ассистент — наставник: показывает факты о детали и спрашивает, что выбрали бы вы, " +
        "разбирает ваши настройки и закрепляет знания короткими квизами.",
      examples: [
        ["Научи меня готовить шестерню к печати", "Прикреплю STL — веди по шагам, решения за мной"],
        ["Проверь мои настройки", "Я сам выставил заполнение и стенки"],
        ["/quiz", "Пара вопросов, чтобы проверить себя"],
        ["Когда печатать в 5D, а когда в 3D?", "Объясните на примерах"]
      ]
    },
    novice: {
      badge: "Режим: новичок",
      title: "Расскажите, что хотите напечатать",
      text: "Пишите своими словами — ассистент задаст пару вопросов, сам подберёт материал и настройки " +
        "и объяснит всё простым языком. Можно просто спросить, как устроена печать.",
      examples: [
        ["Хочу напечатать крепкий держатель", "Расскажу, где он будет висеть, — остальное на вас"],
        ["Что такое заполнение и сколько ставить?", "Объясните простыми словами"],
        ["Почему у детали отслаиваются углы?", "И как это исправить в настройках"],
        ["Я новичок, помогите подготовить модель", "Прикреплю STL — подскажите по шагам"]
      ]
    },
    engineer: {
      badge: "Режим: инженер",
      title: "Опишите задачу печати",
      text: "Приложите STL/OBJ/3MF или ТЗ. Ассистент выберет режим 3D/5D, материал, профиль и ключевые " +
        "настройки, сориентирует модель, нарежет её, сохранит G-code и 3MF и обоснует решения.",
      examples: [
        ["Кронштейн под нагрузку, PETG, без поддержек", "Типовая задача из кейса"],
        ["Деталь по ТЗ: подбери режим, материал и настройки", "Нарежь и сохрани проект"],
        ["Сравни профили Fine и Normal", "По времени печати и расходу материала"],
        ["Проанализируй геометрию модели", "И предложи ориентацию для 3D или 5D"]
      ]
    },
    pro: {
      badge: "Режим: профи",
      title: "Быстро: рецепты и команды",
      text: "Ответ — таблица решений без пояснений. Правки — командами /… за секунду без нейросети, " +
        "повторные детали — по рецептам одной командой.",
      examples: [
        ["/recipes", "Сохранённые рецепты"],
        ["/recipe ", "Применить рецепт к модели на столе"],
        ["/state", "Режим, материал, профиль и ключевые настройки"],
        ["/help", "Все быстрые команды"]
      ]
    }
  };

  // ---------- запросы к плагину ----------
  function api(path, options) {
    options = options || {};
    var headers = { "X-Auth-Token": token };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: options.method || (options.body !== undefined ? "POST" : "GET"),
      headers: headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined
    }).catch(function () {
      throw new Error("Нет связи с плагином: слайсер закрыт или страница открыта не из меню. " +
        "Запустите STE Slicer и откройте чат через Extensions → AI Assistant → Открыть чат.");
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (response.status === 401) throw new Error("Нет доступа: откройте чат из меню слайсера «Extensions → AI Assistant → Открыть чат».");
        if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
        return data;
      });
    });
  }

  // ---------- безопасный мини-Markdown ----------
  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function inline(text) {
    return text
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  }

  function markdown(source) {
    var lines = escapeHtml(source || "").split(/\r?\n/);
    var html = [];
    var list = null;
    var inCode = false;
    var code = [];
    var table = [];

    function closeList() { if (list) { html.push("</" + list + ">"); list = null; } }
    function flushTable() {
      if (!table.length) return;
      var rows = table.filter(function (row) { return !/^\s*\|?\s*:?-{2,}/.test(row); });
      html.push("<table>" + rows.map(function (row, index) {
        var cells = row.replace(/^\s*\||\|\s*$/g, "").split("|");
        var tag = index === 0 ? "th" : "td";
        return "<tr>" + cells.map(function (c) { return "<" + tag + ">" + inline(c.trim()) + "</" + tag + ">"; }).join("") + "</tr>";
      }).join("") + "</table>");
      table = [];
    }

    lines.forEach(function (line) {
      if (/^```/.test(line)) {
        if (inCode) { html.push("<pre><code>" + code.join("\n") + "</code></pre>"); code = []; }
        else { closeList(); flushTable(); }
        inCode = !inCode;
        return;
      }
      if (inCode) { code.push(line); return; }
      if (/^\s*\|.*\|\s*$/.test(line)) { closeList(); table.push(line); return; }
      flushTable();
      var heading = line.match(/^(#{1,4})\s+(.*)$/);
      var bullet = line.match(/^\s*[-*•]\s+(.*)$/);
      var numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (heading) { closeList(); html.push("<h3>" + inline(heading[2]) + "</h3>"); }
      else if (bullet) { if (list !== "ul") { closeList(); html.push("<ul>"); list = "ul"; } html.push("<li>" + inline(bullet[1]) + "</li>"); }
      else if (numbered) { if (list !== "ol") { closeList(); html.push("<ol>"); list = "ol"; } html.push("<li>" + inline(numbered[1]) + "</li>"); }
      else if (!line.trim()) { closeList(); }
      else { closeList(); html.push("<p>" + inline(line) + "</p>"); }
    });
    if (inCode) html.push("<pre><code>" + code.join("\n") + "</code></pre>");
    closeList();
    flushTable();
    return html.join("");
  }

  // Строки «Варианты: a | b | c» превращаем в кнопки быстрых ответов.
  function splitOptions(text) {
    var groups = [];
    var body = String(text || "").split(/\r?\n/).filter(function (line) {
      var match = line.match(/^\s*[*_`]*\s*Варианты\s*[*_`]*\s*:\s*(.+)$/i);
      if (!match) return true;
      var options = match[1].split("|").map(function (o) { return o.replace(/[`*]/g, "").trim(); }).filter(Boolean);
      if (options.length) groups.push(options);
      return false;
    }).join("\n");
    return { body: body, groups: groups };
  }

  // ---------- отрисовка ----------
  function scrollDown() { chatEl.scrollTop = chatEl.scrollHeight; }

  function add(el) {
    $("welcome").classList.add("hidden");
    messagesEl.appendChild(el);
    scrollDown();
    return el;
  }

  function div(className, html) {
    var el = document.createElement("div");
    el.className = className;
    if (html !== undefined) el.innerHTML = html;
    return el;
  }

  function avatar() {
    var img = document.createElement("img");
    img.src = "favicon.svg";
    img.alt = "";
    img.className = "avatar";
    return img;
  }

  function briefArgs(args) {
    if (!args) return "";
    var parts = [];
    Object.keys(args).forEach(function (key) {
      var value = args[key];
      if (key === "path" && typeof value === "string") value = value.split(/[\\/]/).pop();
      if (typeof value === "object") value = JSON.stringify(value);
      parts.push(key + ": " + value);
    });
    var text = parts.join(", ");
    return text.length > 110 ? text.slice(0, 110) + "…" : text;
  }

  function renderOptions(container, groups, live) {
    groups.forEach(function (options) {
      var row = div("options");
      options.forEach(function (option) {
        var button = document.createElement("button");
        button.textContent = option;
        button.disabled = !live;
        button.onclick = function () {
          if (busyHere) return;
          if (groups.length === 1 || /реши сам/i.test(option)) {
            inputEl.value = option;
            send();
            return;
          }
          Array.prototype.forEach.call(row.children, function (b) { b.classList.remove("picked"); });
          button.classList.add("picked");
          var picked = Array.prototype.map.call(container.querySelectorAll(".options button.picked"), function (b) { return b.textContent; });
          inputEl.value = picked.join("; ");
          autosize();
          inputEl.focus();
        };
        row.appendChild(button);
      });
      container.appendChild(row);
    });
    if (live && groups.length > 1) container.appendChild(div("options-hint", "Выберите варианты и нажмите «Отправить»"));
  }

  function renderEvent(event, live) {
    switch (event.type) {
      case "user": {
        var el = div("msg user");
        el.textContent = event.text || "";
        if (event.attachments && event.attachments.length) {
          var files = div("files");
          event.attachments.forEach(function (name) {
            var chip = div("file");
            chip.textContent = "📎 " + name;
            files.appendChild(chip);
          });
          el.appendChild(files);
        }
        var row = div("row user");
        row.appendChild(el);
        add(row);
        progressEl = null;
        Array.prototype.forEach.call(messagesEl.querySelectorAll(".options button"), function (b) { b.disabled = true; });
        break;
      }
      case "assistant": {
        var parsed = splitOptions(event.text);
        var bubble = div("msg assistant", markdown(parsed.body));
        if (parsed.groups.length) renderOptions(bubble, parsed.groups, live);
        if (event.command_seconds != null) {
          var seconds = event.command_seconds < 0.1 ? "< 0,1" : String(event.command_seconds).replace(".", ",");
          bubble.appendChild(div("fast", "⚡ " + escapeHtml(seconds) + " с · без нейросети"));
        }
        var line = div("row");
        line.appendChild(avatar());
        line.appendChild(bubble);
        add(line);
        break;
      }
      case "assistant_note":
        add(div("msg note", escapeHtml(event.text)));
        break;
      case "thinking":
        $("thinking-text").textContent = "Ассистент думает…";
        break;
      case "tool_call": {
        var card = document.createElement("details");
        card.className = "tool";
        card.innerHTML =
          '<summary><span class="status"><span class="spinner"></span></span>' +
          '<span class="title">' + escapeHtml(event.title || event.name) + "</span>" +
          '<span class="name">' + escapeHtml(event.name) + "</span>" +
          '<span class="brief">' + escapeHtml(briefArgs(event.args)) + "</span>" +
          '<span class="time"></span></summary>' +
          '<div class="label">Аргументы</div><pre>' + escapeHtml(JSON.stringify(event.args || {}, null, 1)) + "</pre>" +
          '<div class="label">Результат</div><pre class="result">…</pre>';
        toolCards[event.call_id] = add(card);
        $("thinking-text").textContent = "Выполняется: " + (event.title || event.name) + "…";
        break;
      }
      case "tool_result": {
        var target = toolCards[event.call_id];
        if (!target) break;
        var status = target.querySelector(".status");
        status.textContent = event.ok ? "✓" : "✗";
        status.style.color = event.ok ? "var(--ok)" : "var(--err)";
        target.querySelector(".time").textContent = event.seconds ? event.seconds + " с" : "";
        target.querySelector(".result").textContent = event.result || "";
        if (!event.ok) target.classList.add("failed");
        break;
      }
      case "progress": {
        if (!progressEl) {
          progressEl = add(div("progress", 'Слайсинг: <span class="pct">0%</span><div class="bar"><div></div></div>'));
        }
        var pct = Math.round((event.value || 0) * 100);
        progressEl.querySelector(".pct").textContent = pct + "%";
        progressEl.querySelector(".bar div").style.width = pct + "%";
        if (pct >= 100) progressEl = null;
        break;
      }
      case "slice_result":
        add(div("result-card",
          '<div><div class="k">Время печати</div><div class="v">' + escapeHtml(event.print_time || "—") + "</div></div>" +
          '<div><div class="k">Материал</div><div class="v">' + escapeHtml(event.weight != null ? event.weight + " г" : "—") + "</div></div>" +
          '<div><div class="k">Режим</div><div class="v">' + escapeHtml(MODE_LABELS[event.mode] || event.mode || "—") + "</div></div>"));
        break;
      case "file_saved":
        add(div("saved", (event.kind === "export_gcode" ? "💾 G-code: " : "💾 Проект 3MF: ") +
          "<code>" + escapeHtml(event.path || "") + "</code>"));
        break;
      case "quiz":
        renderQuiz(event);
        break;
      case "quiz_answer":
        renderQuizAnswer(event);
        break;
      case "quiz_done":
        renderQuizDone(event, live);
        break;
      case "status":
        add(div("msg note", escapeHtml(event.text)));
        break;
      case "error":
        add(div("msg error", "⚠ " + escapeHtml(event.text)));
        break;
    }
  }

  // ---------- квиз: ответ проверяет плагин, не нейросеть ----------
  function renderQuiz(event) {
    var card = div("quiz");
    card.appendChild(div("quiz-topic", "Квиз · " + escapeHtml(event.topic || "")));
    card.appendChild(div("quiz-question", escapeHtml(event.question || "")));
    var row = div("quiz-options");
    (event.options || []).forEach(function (option) {
      var button = document.createElement("button");
      button.textContent = option;
      button.onclick = function () {
        if (card.classList.contains("answered")) return;
        Array.prototype.forEach.call(row.children, function (b) { b.disabled = true; });
        api("/api/quiz", { body: { session: sessionId, event: event.id, choice: option } }).then(function (result) {
          if (!result.ok) throw new Error(result.error || "Ответ не принят");
          pollNow();
        }).catch(function (error) {
          Array.prototype.forEach.call(row.children, function (b) { b.disabled = false; });
          add(div("msg error", "⚠ " + escapeHtml(error.message)));
        });
      };
      row.appendChild(button);
    });
    card.appendChild(row);
    quizCards[event.id] = card;
    add(card);
  }

  function renderQuizAnswer(event) {
    var card = quizCards[event.quiz_event];
    if (!card) return;
    card.classList.add("answered");
    Array.prototype.forEach.call(card.querySelectorAll(".quiz-options button"), function (b) {
      b.disabled = true;
      if (b.textContent === event.answer) b.classList.add("right");
      else if (b.textContent === event.choice) b.classList.add("wrong");
    });
    var score = event.score || {};
    card.appendChild(div("quiz-result " + (event.correct ? "ok" : "err"),
      "<b>" + (event.correct ? "Верно." : "Неверно.") + "</b> " + escapeHtml(event.why || "") +
      (score.total ? '<div class="quiz-score">Решено ' + score.solved + " из " + score.total +
        " · уровень: " + escapeHtml(score.level) + "</div>" : "")));
    scrollDown();
  }

  // Итог серии вопросов и кнопки «что дальше»: ещё по теме, другая тема или назад к детали.
  function renderQuizDone(event, live) {
    var card = div("quiz quiz-done");
    var score = event.score || {};
    card.appendChild(div("quiz-question", "Итог: " + event.right + " из " + event.total + " верно" +
      (event.right === event.total ? ". Отлично!" : ". Разберём ещё?")));
    if (score.total) card.appendChild(div("quiz-score", "Всего решено " + score.solved + " из " + score.total +
      " · уровень: " + escapeHtml(score.level)));
    var row = div("options");
    var choices = [];
    if (event.topic) choices.push(["Ещё вопросы: " + (event.topic_label || ""), "/quiz " + event.topic]);
    choices.push(["Другая тема", "/quiz"]);
    choices.push(["Вернуться к детали", "Продолжай работу над деталью"]);
    choices.forEach(function (choice) {
      var button = document.createElement("button");
      button.textContent = choice[0];
      button.disabled = !live;
      button.onclick = function () {
        if (busyHere) return;
        Array.prototype.forEach.call(row.children, function (b) { b.disabled = true; });
        inputEl.value = choice[1];
        send();
      };
      row.appendChild(button);
    });
    card.appendChild(row);
    add(card);
  }

  // ---------- быстрые команды ----------
  function loadCommands() {
    api("/api/commands").then(function (data) { commandList = data.commands || []; }).catch(function () {});
  }

  function matchingCommands() {
    var text = inputEl.value;
    if (text.charAt(0) !== "/" || /\s/.test(text)) return [];
    var prefix = text.slice(1).toLowerCase();
    return commandList.filter(function (c) { return c.name.indexOf(prefix) === 0; });
  }

  function renderCommands() {
    var box = $("commands");
    var items = matchingCommands();
    box.innerHTML = "";
    box.classList.toggle("hidden", !items.length);
    items.forEach(function (command, index) {
      var item = document.createElement("button");
      item.className = "command" + (index === 0 ? " first" : "");
      item.innerHTML = "<code>" + escapeHtml(command.usage) + "</code><span>" + escapeHtml(command.description) + "</span>";
      item.onclick = function () { pickCommand(command); };
      box.appendChild(item);
    });
  }

  function pickCommand(command) {
    var takesArgs = command.usage.indexOf(" ") > 0;
    inputEl.value = "/" + command.name + (takesArgs ? " " : "");
    renderCommands();
    autosize();
    inputEl.focus();
    if (!takesArgs) send();
  }

  function clearChat() {
    messagesEl.innerHTML = "";
    toolCards = {};
    quizCards = {};
    progressEl = null;
    lastEventId = 0;
    $("welcome").classList.remove("hidden");
  }

  // ---------- режим и тема ----------
  function applyMode(value) {
    mode = CHAT_MODES.indexOf(value) >= 0 ? value : "engineer";
    store("ste-ai-mode", mode);
    Array.prototype.forEach.call(document.querySelectorAll("#mode-switch button"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-mode") === mode);
    });
    var w = WELCOME[mode];
    $("welcome-badge").textContent = w.badge;
    $("welcome-title").textContent = w.title;
    $("welcome-text").textContent = w.text;
    var box = $("examples");
    box.innerHTML = "";
    w.examples.forEach(function (example) {
      var button = document.createElement("button");
      button.className = "example";
      button.innerHTML = "<b>" + escapeHtml(example[0]) + "</b><span>" + escapeHtml(example[1]) + "</span>";
      button.onclick = function () { inputEl.value = example[0]; autosize(); inputEl.focus(); };
      box.appendChild(button);
    });
    inputEl.placeholder = {
      learning: "Что хотите научиться печатать? Или «Проверь мои настройки»…",
      novice: "Опишите деталь своими словами или спросите что угодно о печати…",
      engineer: "Задача, ТЗ или вопрос. Приложите модель — Enter для отправки",
      pro: "Задача или команда: /recipe, /infill 40, /slice… (/help — все команды)"
    }[mode];
  }

  function applyTheme(value) {
    theme = ["studio", "graphite", "blueprint"].indexOf(value) >= 0 ? value : "studio";
    store("ste-ai-theme", theme);
    document.documentElement.setAttribute("data-theme", theme);
    Array.prototype.forEach.call(document.querySelectorAll(".theme-card"), function (card) {
      card.classList.toggle("active", card.getAttribute("data-theme-pick") === theme);
    });
  }

  // ---------- история чатов ----------
  function timeLabel(seconds) {
    var date = new Date(seconds * 1000);
    var now = new Date();
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    if (date.toDateString() === now.toDateString()) return "сегодня, " + pad(date.getHours()) + ":" + pad(date.getMinutes());
    return pad(date.getDate()) + "." + pad(date.getMonth() + 1) + ", " + pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  function renderSessions(data) {
    var list = $("chat-list");
    list.innerHTML = "";
    var items = data.sessions || [];
    var current = items.filter(function (s) { return s.id === sessionId; })[0];
    $("chat-title").textContent = current ? current.title : "Новый чат";
    if (!items.length) { list.appendChild(div("chat-empty", "Пока нет чатов")); return; }
    items.forEach(function (s) {
      var item = div("chat-item" + (s.id === sessionId ? " active" : ""));
      var meta = div("meta");
      var title = div("title");
      title.textContent = s.title;
      var when = div("time");
      when.textContent = s.messages ? timeLabel(s.updated) : "пустой";
      meta.appendChild(title);
      meta.appendChild(when);
      item.appendChild(meta);
      if (data.busy && data.running === s.id) item.appendChild(div("running"));
      var del = document.createElement("button");
      del.className = "del";
      del.title = "Удалить чат";
      del.textContent = "×";
      del.onclick = function (event) {
        event.stopPropagation();
        if (!confirm("Удалить чат «" + s.title + "»?")) return;
        api("/api/sessions/delete", { body: { id: s.id } }).then(function (result) {
          if (!result.ok) throw new Error(result.error);
          openSession(result.session);
        }).catch(function (error) { add(div("msg error", "⚠ " + escapeHtml(error.message))); });
      };
      item.appendChild(del);
      item.onclick = function () {
        document.body.classList.remove("side-open");
        if (s.id !== sessionId) api("/api/sessions/select", { body: { id: s.id } }).then(function () { openSession(s.id); });
      };
      list.appendChild(item);
    });
  }

  function refreshSessions() {
    return api("/api/sessions").then(function (data) {
      if (!sessionId) sessionId = data.current;
      renderSessions(data);
      return data;
    }).catch(function () {});
  }

  function openSession(id) {
    sessionId = id;
    clearChat();
    refreshSessions();
    pollNow();
  }

  // ---------- опрос событий ----------
  var pollTimer = null;
  function pollNow() {
    if (pollTimer) clearTimeout(pollTimer);
    poll();
  }

  function poll() {
    if (!sessionId) { pollTimer = setTimeout(poll, 500); return; }
    var asked = sessionId;
    api("/api/events?after=" + lastEventId + "&session=" + encodeURIComponent(asked)).then(function (data) {
      if (asked !== sessionId) return;
      var events = data.events || [];
      var wasBusy = busyHere;
      events.forEach(function (event) {
        lastEventId = Math.max(lastEventId, event.id);
        renderEvent(event, isLastAssistant(events, event));
      });
      setBusy(!!data.busy && data.running === asked, !!data.busy && data.running !== asked);
      if (events.length && (wasBusy !== busyHere || events.some(function (e) { return e.type === "user" || e.type === "run_end"; }))) refreshSessions();
    }).catch(function (error) {
      showBanner(error.message);
    }).then(function () {
      pollTimer = setTimeout(poll, busyHere ? 400 : 1000);
    });
  }

  // Кнопки ответов активны только у последнего ответа ассистента в чате.
  function isLastAssistant(events, event) {
    for (var i = events.length - 1; i >= 0; i--) {
      if (events[i].type === "user") return false;
      if (events[i].type === "assistant" || events[i].type === "quiz_done") return events[i] === event;
    }
    return false;
  }

  function setBusy(here, elsewhere) {
    busyHere = here;
    $("thinking").classList.toggle("hidden", !here);
    $("btn-stop").classList.toggle("hidden", !here);
    $("busy-other").classList.toggle("hidden", !elsewhere);
    $("btn-send").disabled = here || elsewhere;
    if (here) scrollDown();
  }

  var bannerShown = false;
  function showBanner(text) {
    if (bannerShown) return;
    bannerShown = true;
    add(div("msg error", "⚠ " + escapeHtml(text)));
  }

  // ---------- состояние слайсера ----------
  function refreshState() {
    api("/api/state").then(function (state) {
      if (state.error) return;
      $("chip-printer").textContent = state.printer || "—";
      $("chip-mode").textContent = MODE_LABELS[state.printing_mode] || state.printing_mode || "—";
      var extruder = (state.extruders || [])[0];
      $("chip-material").textContent = extruder ? extruder.material : "—";
      $("chip-quality").textContent = state.quality_name || "—";
    }).catch(function () {});
  }

  // ---------- отправка ----------
  function send() {
    var text = inputEl.value.trim();
    if (busyHere || (!text && !pending.length)) return;
    var payload = {
      text: text,
      session: sessionId,
      mode: mode,
      attachments: pending.map(function (f) { return { name: f.name, data: f.data }; })
    };
    $("btn-send").disabled = true;
    api("/api/message", { body: payload }).then(function (data) {
      if (!data.ok) throw new Error(data.error || "Не удалось отправить");
      inputEl.value = "";
      autosize();
      renderCommands();
      pending = [];
      renderAttachments();
      setBusy(true, false);
      pollNow();
    }).catch(function (error) {
      add(div("msg error", "⚠ " + escapeHtml(error.message)));
      $("btn-send").disabled = false;
    });
  }

  function addFiles(fileList) {
    Array.prototype.forEach.call(fileList, function (file) {
      if (!/\.(stl|obj|3mf|jpe?g|png)$/i.test(file.name)) {
        add(div("msg error", "⚠ Неподдерживаемый файл: " + escapeHtml(file.name)));
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        pending.push({ name: file.name, size: file.size, data: reader.result });
        renderAttachments();
      };
      reader.readAsDataURL(file);
    });
  }

  function renderAttachments() {
    attachmentsEl.innerHTML = "";
    pending.forEach(function (file, index) {
      var chip = div("attachment");
      chip.innerHTML = escapeHtml(file.name) + " <span style='color:var(--muted)'>" + (file.size / 1048576).toFixed(1) + " МБ</span>";
      var remove = document.createElement("button");
      remove.textContent = "×";
      remove.title = "Убрать";
      remove.onclick = function () { pending.splice(index, 1); renderAttachments(); };
      chip.appendChild(remove);
      attachmentsEl.appendChild(chip);
    });
  }

  function autosize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
  }

  // ---------- настройки ----------
  function openSettings() {
    $("cfg-info").textContent = "";
    $("cfg-info").className = "cfg-info";
    api("/api/config").then(function (cfg) {
      $("cfg-url").value = cfg.base_url || "";
      $("cfg-key").value = "";
      $("cfg-key").placeholder = cfg.has_key
        ? "••••••••  ✓ ключ сохранён и скрыт — оставьте поле пустым"
        : "ключ ещё не введён";
      $("cfg-key-state").textContent = cfg.has_key ? "✓ сохранён" : "не задан";
      $("cfg-key-state").className = "key-state " + (cfg.has_key ? "ok" : "missing");
      $("cfg-model").value = cfg.model || "";
      $("cfg-out").value = cfg.output_dir || "";
      $("cfg-ssl").checked = !!cfg.verify_ssl;
      $("cfg-info").textContent = "MCP: " + cfg.mcp_url + " · инструментов: " + (cfg.tools || []).length;
    }).catch(function (error) { $("cfg-info").textContent = error.message; });
    $("settings").classList.remove("hidden");
  }

  function saveSettings() {
    var body = {
      base_url: $("cfg-url").value.trim(),
      model: $("cfg-model").value.trim(),
      output_dir: $("cfg-out").value.trim(),
      verify_ssl: $("cfg-ssl").checked
    };
    if ($("cfg-key").value.trim()) body.api_key = $("cfg-key").value.trim();
    return api("/api/config", { body: body });
  }

  function testSettings() {
    $("cfg-info").className = "cfg-info";
    $("cfg-info").textContent = "Проверяем…";
    saveSettings().then(function () { return api("/api/test", { body: {} }); }).then(function (result) {
      if (!result.ok) throw new Error(result.error);
      var list = $("model-list");
      list.innerHTML = "";
      (result.models || []).forEach(function (id) {
        var option = document.createElement("option");
        option.value = id;
        list.appendChild(option);
      });
      $("cfg-info").className = "cfg-info ok";
      $("cfg-info").textContent = "Подключение работает. Модели: " + (result.models || []).join(", ") +
        (result.model_found ? "" : " — указанная модель не найдена!");
    }).catch(function (error) {
      $("cfg-info").className = "cfg-info err";
      $("cfg-info").textContent = error.message;
    });
  }

  // ---------- обработчики ----------
  $("btn-send").onclick = send;
  $("btn-stop").onclick = function () { api("/api/stop", { body: {} }).catch(function () {}); };
  $("btn-new").onclick = function () {
    document.body.classList.remove("side-open");
    api("/api/sessions/new", { body: {} }).then(function (result) { openSession(result.session); })
      .catch(function (error) { add(div("msg error", "⚠ " + escapeHtml(error.message))); });
  };
  $("btn-menu").onclick = function () { document.body.classList.toggle("side-open"); };
  $("scrim").onclick = function () { document.body.classList.remove("side-open"); };
  $("btn-settings").onclick = function () { document.body.classList.remove("side-open"); openSettings(); };
  $("cfg-cancel").onclick = function () { $("settings").classList.add("hidden"); };
  $("cfg-test").onclick = testSettings;
  $("cfg-save").onclick = function () {
    saveSettings().then(function () { $("settings").classList.add("hidden"); }).catch(function (error) {
      $("cfg-info").className = "cfg-info err";
      $("cfg-info").textContent = error.message;
    });
  };
  $("file-input").onchange = function (event) { addFiles(event.target.files); event.target.value = ""; };
  Array.prototype.forEach.call(document.querySelectorAll("#mode-switch button"), function (button) {
    button.onclick = function () { applyMode(button.getAttribute("data-mode")); };
  });
  Array.prototype.forEach.call(document.querySelectorAll(".theme-card"), function (card) {
    card.onclick = function () { applyTheme(card.getAttribute("data-theme-pick")); };
  });

  inputEl.addEventListener("input", function () { autosize(); renderCommands(); });
  inputEl.addEventListener("keydown", function (event) {
    var commands = matchingCommands();
    if (event.key === "Tab" && commands.length) { event.preventDefault(); pickCommand(commands[0]); return; }
    if (event.key === "Escape") { $("commands").classList.add("hidden"); return; }
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); }
  });

  var dragDepth = 0;
  window.addEventListener("dragenter", function (event) { event.preventDefault(); dragDepth++; $("drop-overlay").classList.remove("hidden"); });
  window.addEventListener("dragleave", function () { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("drop-overlay").classList.add("hidden"); });
  window.addEventListener("dragover", function (event) { event.preventDefault(); });
  window.addEventListener("drop", function (event) {
    event.preventDefault();
    dragDepth = 0;
    $("drop-overlay").classList.add("hidden");
    if (event.dataTransfer && event.dataTransfer.files) addFiles(event.dataTransfer.files);
  });

  applyTheme(theme);
  applyMode(mode);
  if (location.protocol === "file:") {
    showBanner("Страница открыта как файл, а не через плагин. Откройте чат из меню слайсера: Extensions → AI Assistant → Открыть чат.");
  } else if (!token) {
    showBanner("Нет токена сессии: откройте чат из меню слайсера «Extensions → AI Assistant → Открыть чат».");
  }
  loadCommands();
  refreshSessions().then(function () { poll(); });
  refreshState();
  setInterval(refreshState, 4000);
  setInterval(refreshSessions, 5000);
})();
