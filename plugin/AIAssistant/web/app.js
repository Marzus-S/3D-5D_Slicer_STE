// SPA чата: история диалога, вложения, вызовы инструментов, прогресс слайсинга, настройки LLM.
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

  var $ = function (id) { return document.getElementById(id); };
  var messagesEl = $("messages");
  var chatEl = $("chat");
  var inputEl = $("input");
  var attachmentsEl = $("attachments");
  var pending = [];        // вложения, ещё не отправленные
  var lastEventId = 0;
  var busy = false;
  var toolCards = {};      // call_id → элемент карточки
  var progressEl = null;

  var MODE_LABELS = { classic: "3D", cylindrical: "5D Spiral", cylindrical_full: "5D" };

  function api(path, options) {
    options = options || {};
    var headers = { "X-Auth-Token": token };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: options.method || (options.body !== undefined ? "POST" : "GET"),
      headers: headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (response.status === 401) throw new Error("Нет доступа: откройте чат из меню слайсера «AI Assistant → Открыть чат».");
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

  // ---------- отрисовка событий ----------
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

  function renderEvent(event) {
    switch (event.type) {
      case "reset":
        messagesEl.innerHTML = "";
        toolCards = {};
        progressEl = null;
        $("welcome").classList.remove("hidden");
        break;
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
        add(el);
        progressEl = null;
        break;
      }
      case "assistant":
        add(div("msg assistant", markdown(event.text)));
        break;
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
        target.querySelector(".status").textContent = event.ok ? "✓" : "✗";
        target.querySelector(".status").style.color = event.ok ? "var(--ok)" : "var(--err)";
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
          "<span>⏱ <b>" + escapeHtml(event.print_time || "—") + "</b></span>" +
          "<span>⚖ <b>" + escapeHtml(event.weight != null ? event.weight + " г" : "—") + "</b></span>" +
          "<span>Режим: <b>" + escapeHtml(MODE_LABELS[event.mode] || event.mode || "—") + "</b></span>"));
        break;
      case "file_saved":
        add(div("saved", (event.kind === "export_gcode" ? "💾 G-code: " : "💾 Проект 3MF: ") +
          "<code>" + escapeHtml(event.path || "") + "</code>"));
        break;
      case "status":
        add(div("msg note", escapeHtml(event.text)));
        break;
      case "error":
        add(div("msg error", "⚠ " + escapeHtml(event.text)));
        break;
    }
  }

  function setBusy(value) {
    busy = value;
    $("thinking").classList.toggle("hidden", !value);
    $("btn-stop").classList.toggle("hidden", !value);
    $("btn-send").disabled = value;
    if (value) scrollDown();
  }

  function poll() {
    api("/api/events?after=" + lastEventId).then(function (data) {
      (data.events || []).forEach(function (event) {
        lastEventId = Math.max(lastEventId, event.id);
        renderEvent(event);
      });
      setBusy(!!data.busy);
    }).catch(function (error) {
      showBanner(error.message);
    }).then(function () {
      setTimeout(poll, busy ? 400 : 1000);
    });
  }

  var bannerShown = false;
  function showBanner(text) {
    if (bannerShown) return;
    bannerShown = true;
    add(div("msg error", "⚠ " + escapeHtml(text)));
  }

  // ---------- состояние слайсера ----------
  function refreshState() {
    if (busy && document.hidden) return;
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
    if (busy || (!text && !pending.length)) return;
    var payload = { text: text, attachments: pending.map(function (f) { return { name: f.name, data: f.data }; }) };
    $("btn-send").disabled = true;
    api("/api/message", { body: payload }).then(function (data) {
      if (!data.ok) throw new Error(data.error || "Не удалось отправить");
      inputEl.value = "";
      autosize();
      pending = [];
      renderAttachments();
      setBusy(true);
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
      $("cfg-key").placeholder = cfg.has_key ? "ключ сохранён — введите новый, чтобы заменить" : "sk-…";
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
    api("/api/reset", { body: {} }).then(function (result) {
      if (!result.ok) add(div("msg error", "⚠ " + escapeHtml(result.error)));
    });
  };
  $("btn-settings").onclick = openSettings;
  $("cfg-cancel").onclick = function () { $("settings").classList.add("hidden"); };
  $("cfg-test").onclick = testSettings;
  $("cfg-save").onclick = function () {
    saveSettings().then(function () { $("settings").classList.add("hidden"); }).catch(function (error) {
      $("cfg-info").className = "cfg-info err";
      $("cfg-info").textContent = error.message;
    });
  };
  $("file-input").onchange = function (event) { addFiles(event.target.files); event.target.value = ""; };

  inputEl.addEventListener("input", autosize);
  inputEl.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); }
  });

  Array.prototype.forEach.call(document.querySelectorAll(".example"), function (button) {
    button.onclick = function () { inputEl.value = button.textContent; autosize(); inputEl.focus(); };
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

  if (!token) showBanner("Нет токена сессии: откройте чат из меню слайсера «AI Assistant → Открыть чат».");
  poll();
  refreshState();
  setInterval(refreshState, 4000);
})();
