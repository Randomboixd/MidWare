(function () {
  "use strict";

  var form = document.getElementById("premodel-form");
  if (!form) {
    return;
  }

  var nameInput = document.getElementById("premodel-name");
  var slugInput = document.getElementById("premodel-slug");
  var description = document.getElementById("premodel-description");
  var descriptionCount = document.getElementById("description-count");
  var routeSelect = document.getElementById("premodel-route");
  var modelList = document.getElementById("premodel-models");
  var paramsEnabled = document.getElementById("params-enabled");
  var paramsPanel = document.getElementById("params-panel");
  var simplePanel = document.getElementById("simple-panel");
  var presetPanel = document.getElementById("preset-panel");
  var presetFile = document.getElementById("preset-file");
  var presetJson = document.getElementById("preset-json");
  var presetPrompts = document.getElementById("preset-prompts");
  var presetStatus = document.getElementById("preset-status");

  var prompts = [];
  var slugTouched = Boolean(slugInput.value.trim());

  function slugify(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[\s_]+/g, "-")
      .replace(/[^a-z0-9.-]+/g, "")
      .replace(/-{2,}/g, "-")
      .replace(/^[-.]+|[-.]+$/g, "");
  }

  function toInt(value, fallback) {
    var parsed = parseInt(value, 10);
    return isNaN(parsed) ? fallback : parsed;
  }

  function normalizePreset(raw) {
    if (!raw || typeof raw !== "object") {
      return { prompts: [] };
    }
    var enabledMap = {};
    if (Array.isArray(raw.prompt_order)) {
      raw.prompt_order.forEach(function (group) {
        if (!group || !Array.isArray(group.order)) {
          return;
        }
        group.order.forEach(function (item) {
          if (item && item.identifier !== undefined && item.identifier !== null) {
            enabledMap[String(item.identifier)] = item.enabled !== false;
          }
        });
      });
    }

    var normalized = [];
    if (Array.isArray(raw.prompts)) {
      raw.prompts.forEach(function (item) {
        if (!item || typeof item !== "object") {
          return;
        }
        var identifier = String(item.identifier || item.id || item.name || "");
        var enabled;
        if (Object.prototype.hasOwnProperty.call(enabledMap, identifier)) {
          enabled = enabledMap[identifier];
        } else if (Object.prototype.hasOwnProperty.call(item, "enabled")) {
          enabled = Boolean(item.enabled);
        } else {
          enabled = !item.marker;
        }
        var role = String(item.role || "").toLowerCase();
        if (role !== "system" && role !== "user" && role !== "assistant") {
          role = item.system_prompt ? "system" : "user";
        }
        normalized.push({
          identifier: identifier,
          name: String(item.name || identifier || ""),
          role: role,
          content: String(item.content || ""),
          system_prompt: Boolean(item.system_prompt),
          marker: Boolean(item.marker),
          injection_position: toInt(item.injection_position, 0),
          injection_depth: toInt(item.injection_depth, 0),
          injection_order: toInt(item.injection_order, 100),
          enabled: enabled
        });
      });
    }
    return { prompts: normalized };
  }

  function syncJson() {
    presetJson.value = JSON.stringify({ prompts: prompts });
  }

  function render() {
    presetPrompts.textContent = "";
    if (!prompts.length) {
      presetStatus.textContent = "No prompts found in this preset.";
      return;
    }
    var enabled = prompts.filter(function (prompt) {
      return prompt.enabled;
    }).length;
    presetStatus.textContent = prompts.length + " prompt(s), " + enabled + " enabled.";

    prompts.forEach(function (prompt, index) {
      var row = document.createElement("label");
      row.className = "preset-prompt" + (prompt.content ? "" : " is-empty");
      var box = document.createElement("input");
      box.type = "checkbox";
      box.checked = Boolean(prompt.enabled);
      box.addEventListener("change", function () {
        prompts[index].enabled = box.checked;
        syncJson();
        render();
      });
      var title = document.createElement("span");
      title.className = "preset-prompt-name";
      title.textContent = prompt.name || prompt.identifier || "(unnamed)";
      var meta = document.createElement("small");
      meta.className = "preset-prompt-meta";
      var bits = [prompt.role];
      if (prompt.role !== "system" && !prompt.system_prompt) {
        bits.push("depth " + prompt.injection_depth);
        bits.push("order " + prompt.injection_order);
      }
      meta.textContent = bits.join(" · ");
      row.appendChild(box);
      row.appendChild(title);
      row.appendChild(meta);
      presetPrompts.appendChild(row);
    });
  }

  function loadPreset(raw, source) {
    var result = normalizePreset(raw);
    prompts = result.prompts;
    syncJson();
    render();
    if (source) {
      presetStatus.textContent = source + " · " + presetStatus.textContent;
    }
  }

  nameInput.addEventListener("input", function () {
    if (!slugTouched) {
      slugInput.value = slugify(nameInput.value);
    }
  });

  slugInput.addEventListener("input", function () {
    slugTouched = slugInput.value.trim().length > 0;
  });

  function updateDescriptionCount() {
    descriptionCount.textContent = String(description.value.length);
  }
  description.addEventListener("input", updateDescriptionCount);
  updateDescriptionCount();

  var modelOptions = modelList ? Array.prototype.slice.call(modelList.options) : [];
  function filterModels() {
    if (!modelList || !routeSelect) {
      return;
    }
    var routeId = routeSelect.value;
    modelList.textContent = "";
    modelOptions.forEach(function (option) {
      if (!routeId || option.dataset.route === routeId) {
        modelList.appendChild(option.cloneNode(true));
      }
    });
  }
  if (routeSelect) {
    routeSelect.addEventListener("change", filterModels);
    filterModels();
  }

  function updatePanels() {
    var mode = form.querySelector('input[name="prompt_mode"]:checked');
    var isPreset = mode && mode.value === "sillytavern";
    simplePanel.hidden = isPreset;
    presetPanel.hidden = !isPreset;
  }
  form.querySelectorAll('input[name="prompt_mode"]').forEach(function (radio) {
    radio.addEventListener("change", updatePanels);
  });
  updatePanels();

  function updateParamsPanel() {
    if (paramsPanel) {
      paramsPanel.hidden = !(paramsEnabled && paramsEnabled.checked);
    }
  }
  if (paramsEnabled) {
    paramsEnabled.addEventListener("change", updateParamsPanel);
    updateParamsPanel();
  }

  function prefillParams(raw) {
    if (!raw || typeof raw !== "object") {
      return;
    }
    ["temperature", "top_p", "max_tokens"].forEach(function (key) {
      var input = document.getElementById(key);
      if (!input || input.value !== "" || raw[key] === undefined || raw[key] === null) {
        return;
      }
      input.value = raw[key];
    });
  }

  presetFile.addEventListener("change", function () {
    var file = presetFile.files && presetFile.files[0];
    if (!file) {
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var raw = JSON.parse(reader.result);
        prefillParams(raw);
        loadPreset(raw, file.name);
      } catch (error) {
        presetStatus.textContent = "Could not parse " + file.name + ": " + error.message;
      }
    };
    reader.readAsText(file);
  });

  var inline = document.getElementById("preset-data");
  if (inline) {
    try {
      var parsed = JSON.parse(inline.textContent);
      if (parsed && Array.isArray(parsed.prompts) && parsed.prompts.length) {
        loadPreset(parsed, "Loaded");
      }
    } catch (error) {
      // An empty or malformed inline payload simply leaves the editor blank.
    }
  }
})();
