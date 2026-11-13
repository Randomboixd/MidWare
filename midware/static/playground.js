/* MidWare Playground: build a chat request in the browser and run it through
 * this instance's own /v1. The docs panel renders the same payload the Generate
 * button posts, from one buildPayload(), so they cannot drift. */
(function () {
  "use strict";

  var root = document.getElementById("pg-grid");
  if (!root) {
    return;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  var API_BASE = (root.getAttribute("data-api-base") || window.location.origin + "/v1")
    .replace(/\/+$/, "");
  var CHAT_URL = API_BASE + "/chat/completions";
  var MODELS_URL = API_BASE + "/models";
  var KEY_URL = "/admin/playground/key";
  var STORE_KEY = "midware.playground.v1";
  var ROLES = ["system", "user", "assistant", "developer"];
  var SAVE_DELAY = 300;

  var els = {
    grid: root,
    blocks: byId("pg-blocks"),
    add: byId("pg-add"),
    model: byId("pg-model"),
    response: byId("pg-response"),
    meta: byId("pg-meta"),
    generate: byId("pg-generate"),
    stop: byId("pg-stop"),
    append: byId("pg-append"),
    requests: byId("pg-requests"),
    extras: byId("pg-extras"),
    code: byId("pg-code"),
    tabs: byId("pg-tabs"),
    copy: byId("pg-copy")
  };

  if (!els.blocks || !els.generate) {
    return;
  }

  var state = {
    blocks: [],
    model: "",
    extras: "",
    tab: "curl",
    key: null,
    busy: false,
    reply: "",
    controller: null,
    usage: null,
    finishReason: "",
    lastStatus: 0,
    requestsUrl: ""
  };

  // -- persistence ---------------------------------------------------------

  function loadState() {
    var raw = null;
    try {
      raw = window.localStorage.getItem(STORE_KEY);
    } catch (err) {
      raw = null;
    }
    var saved = null;
    if (raw) {
      try {
        saved = JSON.parse(raw);
      } catch (err) {
        saved = null;
      }
    }
    if (saved && Array.isArray(saved.blocks) && saved.blocks.length) {
      state.blocks = saved.blocks.map(function (block) {
        return {
          role: ROLES.indexOf(block.role) >= 0 ? block.role : "user",
          content: typeof block.content === "string" ? block.content : ""
        };
      });
      state.model = typeof saved.model === "string" ? saved.model : "";
      state.extras = typeof saved.extras === "string" ? saved.extras : "";
    } else {
      state.blocks = DEFAULT_BLOCKS.map(function (block) {
        return { role: block.role, content: block.content };
      });
    }
    if (els.extras) {
      els.extras.value = state.extras;
    }
  }

  var saveTimer = null;

  function scheduleSave() {
    if (saveTimer) {
      window.clearTimeout(saveTimer);
    }
    saveTimer = window.setTimeout(function () {
      saveTimer = null;
      try {
        window.localStorage.setItem(
          STORE_KEY,
          JSON.stringify({
            blocks: state.blocks,
            model: state.model,
            extras: state.extras
          })
        );
      } catch (err) {
        /* private mode or a full quota: autosave is a convenience, never a failure */
      }
    }, SAVE_DELAY);
  }

  // -- blocks --------------------------------------------------------------

  var PLACEHOLDERS = {
    system: "You are…",
    user: "Your message",
    assistant: "A reply to prime the conversation",
    developer: "Developer instructions"
  };

  // First-visit blocks. The system prompt is deliberately not "you are a helpful
  // assistant": a playground should start with something you want to tweak, and
  // this one is a joke that makes the empty state worth reading.
  var DEFAULT_BLOCKS = [
    {
      role: "system",
      content:
        "You're a very helpful and kind assistant who is totally secretly an " +
        "eldritch god waiting for you to make the mistake of hitting Generate on " +
        "this API Playground page! ...\n\nOf course this is all fake...\n\n... Unless..."
    },
    { role: "user", content: "i like turtles" }
  ];

  function autoGrow(area) {
    area.style.height = "auto";
    area.style.height = Math.min(area.scrollHeight + 2, 260) + "px";
  }

  function renderBlocks() {
    els.blocks.innerHTML = "";
    state.blocks.forEach(function (block, index) {
      var card = document.createElement("div");
      card.className = "pg-block";
      card.setAttribute("data-index", String(index));

      var head = document.createElement("div");
      head.className = "pg-block-head";

      var number = document.createElement("span");
      number.className = "pg-index";
      number.textContent = String(index);
      head.appendChild(number);

      var roleField = document.createElement("div");
      roleField.className = "field border small pg-role-field";
      var roleSelect = document.createElement("select");
      roleSelect.className = "pg-role";
      roleSelect.title = "Message role";
      roleSelect.setAttribute("aria-label", "Role for message " + index);
      ROLES.forEach(function (role) {
        var option = document.createElement("option");
        option.value = role;
        option.textContent = role;
        option.selected = role === block.role;
        roleSelect.appendChild(option);
      });
      roleField.appendChild(roleSelect);
      head.appendChild(roleField);

      var spacer = document.createElement("span");
      spacer.className = "pg-spacer";
      head.appendChild(spacer);

      var handle = document.createElement("span");
      handle.className = "pg-drag";
      handle.setAttribute("draggable", "true");
      handle.title = "Drag to reorder";
      handle.setAttribute("aria-hidden", "true");
      handle.textContent = "\u28FF";
      head.appendChild(handle);

      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "pg-del";
      remove.setAttribute("data-action", "delete");
      remove.title = "Delete this block";
      remove.textContent = "\u2715";
      head.appendChild(remove);

      var area = document.createElement("textarea");
      area.className = "pg-text";
      area.spellcheck = false;
      area.rows = 2;
      area.value = block.content;
      area.placeholder = PLACEHOLDERS[block.role] || "";
      area.setAttribute("aria-label", block.role + " message " + index);
      area.addEventListener("input", function () {
        state.blocks[index].content = area.value;
        autoGrow(area);
        scheduleSave();
        scheduleDocs();
      });

      card.appendChild(head);
      card.appendChild(area);
      els.blocks.appendChild(card);
      autoGrow(area);
    });
  }

  function blocksChanged() {
    renderBlocks();
    scheduleSave();
    scheduleDocs();
  }

  els.blocks.addEventListener("click", function (event) {
    var button = event.target.closest("[data-action]");
    if (!button) {
      return;
    }
    var card = button.closest(".pg-block");
    if (!card) {
      return;
    }
    var index = Number(card.getAttribute("data-index"));
    if (!state.blocks[index]) {
      return;
    }
    if (button.getAttribute("data-action") === "delete") {
      state.blocks.splice(index, 1);
      blocksChanged();
    }
  });

  els.blocks.addEventListener("change", function (event) {
    var select = event.target.closest(".pg-role");
    if (!select) {
      return;
    }
    var card = select.closest(".pg-block");
    var index = Number(card.getAttribute("data-index"));
    if (!state.blocks[index]) {
      return;
    }
    state.blocks[index].role = select.value;
    var area = card.querySelector(".pg-text");
    if (area) {
      area.placeholder = PLACEHOLDERS[select.value] || "";
      area.setAttribute("aria-label", select.value + " message " + index);
    }
    scheduleSave();
    scheduleDocs();
  });

  if (els.add) {
    els.add.addEventListener("click", function () {
      state.blocks.push({ role: "user", content: "" });
      blocksChanged();
      var areas = els.blocks.querySelectorAll(".pg-text");
      var last = areas[areas.length - 1];
      if (last) {
        last.focus();
      }
    });
  }

  // Drag by the handle only: making the whole card draggable would eat text
  // selection inside the textarea, which is the one thing people do most here.
  var dragIndex = null;

  els.blocks.addEventListener("dragstart", function (event) {
    var handle = event.target.closest(".pg-drag");
    if (!handle) {
      return;
    }
    var card = handle.closest(".pg-block");
    dragIndex = Number(card.getAttribute("data-index"));
    card.classList.add("pg-dragging");
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(dragIndex));
    }
  });

  els.blocks.addEventListener("dragend", function () {
    dragIndex = null;
    Array.prototype.forEach.call(els.blocks.querySelectorAll(".pg-block"), function (card) {
      card.classList.remove("pg-dragging", "pg-drop-before", "pg-drop-after");
    });
  });

  function dropTarget(event) {
    var card = event.target.closest(".pg-block");
    if (!card || dragIndex === null) {
      return null;
    }
    var box = card.getBoundingClientRect();
    return {
      index: Number(card.getAttribute("data-index")),
      after: event.clientY > box.top + box.height / 2,
      card: card
    };
  }

  els.blocks.addEventListener("dragover", function (event) {
    var target = dropTarget(event);
    if (!target) {
      return;
    }
    event.preventDefault();
    Array.prototype.forEach.call(els.blocks.querySelectorAll(".pg-block"), function (card) {
      card.classList.remove("pg-drop-before", "pg-drop-after");
    });
    target.card.classList.add(target.after ? "pg-drop-after" : "pg-drop-before");
  });

  els.blocks.addEventListener("drop", function (event) {
    var target = dropTarget(event);
    if (!target) {
      return;
    }
    event.preventDefault();
    var from = dragIndex;
    var to = target.after ? target.index + 1 : target.index;
    if (from === null || from === to || from === to - 1) {
      return;
    }
    var moved = state.blocks.splice(from, 1)[0];
    if (from < to) {
      to -= 1;
    }
    state.blocks.splice(to, 0, moved);
    dragIndex = null;
    blocksChanged();
  });

  // -- payload + docs ------------------------------------------------------

  function messages() {
    return state.blocks
      .filter(function (block) {
        return block.content.trim() !== "";
      })
      .map(function (block) {
        return { role: block.role, content: block.content };
      });
  }

  function buildPayload() {
    var msgs = messages();
    if (!msgs.length) {
      throw new Error("Add some text to a message block first.");
    }
    if (!state.model) {
      throw new Error("Pick a model first.");
    }
    var body = {};
    var raw = (els.extras ? els.extras.value : "").trim();
    if (raw) {
      var parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (err) {
        throw new Error("Extra parameters are not valid JSON: " + err.message);
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Extra parameters must be a JSON object.");
      }
      Object.keys(parsed).forEach(function (key) {
        body[key] = parsed[key];
      });
    }
    // These three come from the visible blocks last, so the previewed call and
    // the real one cannot disagree even if extras mention them.
    body.model = state.model;
    body.messages = msgs;
    body.stream = true;
    return body;
  }

  // The key that talks to /v1 is the page's own business: it is never shown, so
  // the snippets use a placeholder. What you copy is meant for your client.
  function currentKey() {
    return "<your-midware-key>";
  }

  function shellQuote(value) {
    return "'" + String(value).replace(/'/g, "'\\''") + "'";
  }

  function pyStr(value) {
    return JSON.stringify(String(value));
  }

  function pyMessages(msgs) {
    return (
      "[\n" +
      msgs
        .map(function (msg) {
          return "        {\"role\": " + pyStr(msg.role) + ", \"content\": " + pyStr(msg.content) + "}";
        })
        .join(",\n") +
      ",\n    ]"
    );
  }

  function snippets(body) {
    var model = JSON.stringify(body.model);
    var pretty = JSON.stringify(body, null, 2);
    return {
      curl:
        "curl " + API_BASE + "/chat/completions \\\n" +
        "  -H \"Content-Type: application/json\" \\\n" +
        "  -H \"Authorization: Bearer " + currentKey() + "\" \\\n" +
        "  -d " + shellQuote(payloadOneLine(body)) + "\n",
      python:
        "from openai import OpenAI\n\n" +
        "client = OpenAI(\n" +
        "    base_url=" + pyStr(API_BASE) + ",\n" +
        "    api_key=" + pyStr(currentKey()) + ",\n" +
        ")\n\n" +
        "stream = client.chat.completions.create(\n" +
        "    model=" + pyStr(body.model) + ",\n" +
        "    messages=" + pyMessages(body.messages) + ",\n" +
        "    stream=True,\n" +
        ")\n\n" +
        "for chunk in stream:\n" +
        "    print(chunk.choices[0].delta.content or \"\", end=\"\", flush=True)\n",
      js:
        "const res = await fetch(" + JSON.stringify(CHAT_URL) + ", {\n" +
        "  method: \"POST\",\n" +
        "  headers: {\n" +
        "    \"Content-Type\": \"application/json\",\n" +
        "    \"Authorization\": \"Bearer " + currentKey() + "\",\n" +
        "  },\n" +
        "  body: JSON.stringify(" + indent(pretty, 2) + "),\n" +
        "});\n\n" +
        "const reader = res.body.getReader();\n" +
        "const decoder = new TextDecoder();\n" +
        "let buffer = \"\";\n" +
        "let text = \"\";\n\n" +
        "while (true) {\n" +
        "  const { value, done } = await reader.read();\n" +
        "  if (done) break;\n" +
        "  buffer += decoder.decode(value, { stream: true });\n" +
        "  const events = buffer.split(\"\\n\\n\");\n" +
        "  buffer = events.pop();\n" +
        "  for (const event of events) {\n" +
        "    for (const line of event.split(\"\\n\")) {\n" +
        "      if (!line.startsWith(\"data: \") || line.slice(6).trim() === \"[DONE]\") continue;\n" +
        "      const chunk = JSON.parse(line.slice(6));\n" +
        "      text += chunk.choices?.[0]?.delta?.content ?? \"\";\n" +
        "    }\n" +
        "  }\n" +
        "}\n\n" +
        "console.log(text);\n",
      body: pretty + "\n",
      model: model
    };
  }

  // The curl snippet is one line because here-strings and multiline -d values
  // are the classic way a copied snippet fails on the first try.
  function payloadOneLine(body) {
    return JSON.stringify(body);
  }

  function indent(text, spaces) {
    var pad = new Array(spaces + 1).join(" ");
    return text
      .split("\n")
      .map(function (line, index) {
        return index === 0 ? line : pad + line;
      })
      .join("\n");
  }

  function currentDoc() {
    if (!els.code) {
      return "";
    }
    var body;
    try {
      body = buildPayload();
    } catch (err) {
      if (state.tab === "body" || state.tab === "curl" || state.tab === "python" || state.tab === "js") {
        return "// " + err.message + "\n";
      }
      return "";
    }
    var all = snippets(body);
    return all[state.tab] || all.curl;
  }

  var docsTimer = null;

  function scheduleDocs() {
    if (docsTimer) {
      window.clearTimeout(docsTimer);
    }
    docsTimer = window.setTimeout(updateDocs, 120);
  }

  function updateDocs() {
    docsTimer = null;
    if (els.code) {
      els.code.textContent = currentDoc();
    }
  }

  function selectTab(tab) {
    state.tab = tab;
    if (els.tabs) {
      Array.prototype.forEach.call(els.tabs.querySelectorAll(".pg-tab"), function (button) {
        var active = button.getAttribute("data-tab") === tab;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      });
    }
    updateDocs();
  }

  if (els.tabs) {
    els.tabs.addEventListener("click", function (event) {
      var button = event.target.closest(".pg-tab");
      if (button) {
        selectTab(button.getAttribute("data-tab"));
      }
    });
  }

  // -- clipboard -----------------------------------------------------------

  function copyText(text) {
    return window.mwCopy(text);
  }

  function flash(button, text) {
    window.mwFlash(button, text);
  }

  if (els.copy) {
    els.copy.addEventListener("click", function () {
      copyText(els.code ? els.code.textContent : "").then(function (ok) {
        flash(els.copy, ok ? "Copied" : "Copy failed");
      });
    });
  }

  // -- the temporary key + model catalogue ---------------------------------

  function loadKey() {
    return fetch(KEY_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    })
      .then(function (res) {
        if (!res.ok) {
          throw new Error("could not get a temporary key (" + res.status + ")");
        }
        return res.json();
      })
      .then(function (data) {
        state.key = data;
        if (els.requests && data.requests_url) {
          els.requests.setAttribute("href", data.requests_url);
          state.requestsUrl = data.requests_url;
        }
        return data;
      });
  }

  function loadModels() {
    var key = state.key ? state.key.token : "";
    return fetch(MODELS_URL, { headers: { Authorization: "Bearer " + key } })
      .then(function (res) {
        return res.json().then(function (body) {
          if (!res.ok) {
            throw new Error(errorMessage(body, res.status));
          }
          return body;
        });
      })
      .then(function (body) {
        var models = (body && body.data ? body.data : []).slice();
        if (!els.model) {
          return;
        }
        els.model.innerHTML = "";
        if (!models.length) {
          var empty = document.createElement("option");
          empty.value = "";
          empty.textContent = "no models — check your routes";
          els.model.appendChild(empty);
          return;
        }
        var order = [];
        var groups = {};
        models.forEach(function (model) {
          var owner = model.owned_by || "models";
          if (!groups[owner]) {
            groups[owner] = [];
            order.push(owner);
          }
          groups[owner].push(model.id);
        });
        order.forEach(function (owner) {
          var group = document.createElement("optgroup");
          group.label = owner;
          groups[owner].forEach(function (id) {
            var option = document.createElement("option");
            option.value = id;
            option.textContent = id;
            group.appendChild(option);
          });
          els.model.appendChild(group);
        });
        var known = models.some(function (model) {
          return model.id === state.model;
        });
        els.model.value = known ? state.model : models[0].id;
        state.model = els.model.value;
        scheduleSave();
        updateDocs();
      });
  }

  if (els.model) {
    els.model.addEventListener("change", function () {
      state.model = els.model.value;
      scheduleSave();
      updateDocs();
    });
  }

  if (els.extras) {
    els.extras.addEventListener("input", function () {
      state.extras = els.extras.value;
      scheduleSave();
      scheduleDocs();
    });
  }

  var extrasToggle = byId("pg-extras-toggle");
  var extrasPanel = byId("pg-extras-panel");
  if (extrasToggle && extrasPanel) {
    extrasToggle.addEventListener("click", function () {
      var open = extrasPanel.hidden;
      extrasPanel.hidden = !open;
      extrasToggle.setAttribute("aria-expanded", open ? "true" : "false");
      extrasToggle.classList.toggle("open", open);
    });
  }

  // -- generation ----------------------------------------------------------

  function errorMessage(body, status) {
    if (body && body.error && typeof body.error === "object" && body.error.message) {
      return body.error.message + " (" + status + ")";
    }
    if (body && typeof body.error === "string") {
      return body.error + " (" + status + ")";
    }
    return "request failed (" + status + ")";
  }

  function setBusy(busy) {
    state.busy = busy;
    if (els.generate) {
      els.generate.disabled = busy;
      els.generate.textContent = busy ? "Generating…" : "Generate";
    }
    if (els.stop) {
      els.stop.hidden = !busy;
    }
  }

  function showError(message) {
    els.response.textContent = String(message);
    els.response.classList.add("pg-error");
    els.meta.textContent = "error";
  }

  function clearError() {
    els.response.classList.remove("pg-error");
  }

  function appendPiece(piece) {
    state.reply += piece;
    els.response.textContent = state.reply;
    els.response.scrollTop = els.response.scrollHeight;
  }

  function takeEvent(event) {
    if (!event || typeof event !== "object") {
      return;
    }
    if (event.usage) {
      state.usage = event.usage;
    }
    var choice = event.choices && event.choices[0];
    if (!choice) {
      return;
    }
    if (choice.finish_reason) {
      state.finishReason = choice.finish_reason;
    }
    var delta = choice.delta || choice.message || {};
    var piece = typeof delta.content === "string" ? delta.content : "";
    if (piece) {
      appendPiece(piece);
    }
  }

  function consumeSse(raw) {
    var lines = String(raw).split("\n");
    for (var i = 0; i < lines.length; i += 1) {
      var line = lines[i];
      if (line.indexOf("data:") !== 0) {
        continue;
      }
      var payload = line.slice(5).trim();
      if (!payload || payload === "[DONE]") {
        continue;
      }
      var event;
      try {
        event = JSON.parse(payload);
      } catch (err) {
        continue;
      }
      takeEvent(event);
    }
  }

  function finish(elapsed) {
    var bits = [];
    if (state.lastStatus) {
      bits.push("HTTP " + state.lastStatus);
    }
    bits.push(elapsed + " ms");
    if (state.usage) {
      bits.push(
        (state.usage.prompt_tokens || 0) + " in / " +
          (state.usage.completion_tokens || 0) + " out"
      );
    }
    if (state.finishReason) {
      bits.push(state.finishReason);
    }
    els.meta.textContent = bits.join(" · ");
    els.append.hidden = !state.reply;
    if (els.requests) {
      els.requests.hidden = !state.requestsUrl;
    }
  }

  function consumeWhole(text) {
    var body;
    try {
      body = JSON.parse(text);
    } catch (err) {
      appendPiece(text);
      return;
    }
    takeEvent(body);
  }

  function streamResponse(res, controller, started) {
    var reader = res.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";

    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) {
          if (buffer.trim()) {
            consumeSse(buffer);
          }
          finish(Date.now() - started);
          return null;
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        var events = buffer.split("\n\n");
        buffer = events.pop();
        events.forEach(consumeSse);
        return pump();
      });
    }

    return pump();
  }

  function generate() {
    if (state.busy) {
      return;
    }
    if (!state.key || !state.key.token) {
      showError("No temporary key yet. Reload the page if this sticks.");
      return;
    }
    var body;
    try {
      body = buildPayload();
    } catch (err) {
      showError(err.message);
      return;
    }

    clearError();
    state.reply = "";
    state.usage = null;
    state.finishReason = "";
    state.lastStatus = 0;
    els.response.textContent = "";
    els.meta.textContent = "sending…";
    els.append.hidden = true;
    if (els.requests) {
      els.requests.hidden = true;
    }
    setBusy(true);

    var started = Date.now();
    var controller = new AbortController();
    state.controller = controller;

    fetch(CHAT_URL, {
      method: "POST",
      credentials: "same-origin",
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        Authorization: "Bearer " + state.key.token
      },
      body: JSON.stringify(body)
    })
      .then(function (res) {
        state.lastStatus = res.status;
        if (!res.ok) {
          return res.text().then(function (text) {
            var parsed;
            try {
              parsed = JSON.parse(text);
            } catch (err) {
              parsed = null;
            }
            throw new Error(errorMessage(parsed, res.status) + (parsed ? "" : "\n" + text));
          });
        }
        if (!res.body || typeof res.body.getReader !== "function") {
          return res.text().then(function (text) {
            consumeWhole(text);
            finish(Date.now() - started);
            return null;
          });
        }
        return streamResponse(res, controller, started);
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") {
          els.meta.textContent = "stopped after " + (Date.now() - started) + " ms";
          return null;
        }
        showError(err && err.message ? err.message : String(err));
        return null;
      })
      .then(function () {
        state.controller = null;
        setBusy(false);
        updateDocs();
      });
  }

  if (els.generate) {
    els.generate.addEventListener("click", generate);
  }

  if (els.stop) {
    els.stop.addEventListener("click", function () {
      if (state.controller) {
        state.controller.abort();
      }
    });
  }

  if (els.append) {
    els.append.addEventListener("click", function () {
      if (!state.reply) {
        return;
      }
      state.blocks.push({ role: "assistant", content: state.reply });
      blocksChanged();
      els.append.hidden = true;
    });
  }

  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      generate();
    }
  });

  // -- boot ----------------------------------------------------------------

  loadState();
  renderBlocks();
  selectTab("curl");

  loadKey()
    .then(loadModels)
    .catch(function (err) {
      showError(
        (err && err.message ? err.message : String(err)) +
          "\n\nSign in, then reload. If the playground key was deleted on the Keys page, " +
          "this page mints a fresh one on the next load."
      );
    });
})();
