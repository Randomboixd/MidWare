(function () {
  "use strict";

  var form = document.getElementById("premodel-form");
  if (!form) {
    return;
  }
  var checkbox = document.getElementById("use-workflow");
  var editor = document.getElementById("workflow-editor");
  var canvas = document.getElementById("workflow-canvas");
  var hidden = document.getElementById("workflow-json");
  var promptSections = document.getElementById("prompt-sections");
  if (!checkbox || !editor || !canvas || !hidden) {
    return;
  }

  var routeSelect = document.getElementById("premodel-route");
  var modelList = document.getElementById("premodel-models");
  var routes = [];
  if (routeSelect) {
    Array.prototype.forEach.call(routeSelect.options, function (opt) {
      if (opt.value) {
        routes.push({ id: opt.value, name: opt.textContent.trim() });
      }
    });
  }

  var NODE_DEFAULTS = {
    type: "api_request",
    route_id: null,
    model: "",
    system_prompt: "",
    prompt: "",
    expect_json: false,
    image_mode: "batch",
    strip_images: false,
    max_tokens: null,
    timeout_seconds: 60
  };
  var NODE_TYPE_LABELS = {
    api_request: "API Request",
    image_captioner: "Image Captioner"
  };
  var dragState = null;
  var graph = defaultGraph();

  function defaultGraph() {
    return {
      version: 1,
      reasoning: false,
      final: { messages: [{ kind: "core_context" }], model_override: "" },
      nodes: []
    };
  }

  function cloneDefaults(type) {
    var node = {};
    Object.keys(NODE_DEFAULTS).forEach(function (key) {
      node[key] = NODE_DEFAULTS[key];
    });
    node.type = type || "api_request";
    if (node.type === "image_captioner") {
      node.prompt = "Describe this image in detail.";
    }
    return node;
  }

  function loadGraph() {
    var seed = document.getElementById("workflow-data");
    if (seed) {
      try {
        var parsed = JSON.parse(seed.textContent);
        if (parsed && typeof parsed === "object" && Array.isArray(parsed.nodes)) {
          if (!parsed.final || typeof parsed.final !== "object") {
            parsed.final = defaultGraph().final;
          }
          if (!Array.isArray(parsed.final.messages) || !parsed.final.messages.length) {
            parsed.final.messages = [{ kind: "core_context" }];
          }
          parsed.version = 1;
          parsed.reasoning = Boolean(parsed.reasoning);
          parsed.nodes.forEach(function (node) {
            if (!NODE_TYPE_LABELS[node.type]) {
              node.type = "api_request";
            }
            if (node.image_mode !== "individual" && node.image_mode !== "batch") {
              node.image_mode = "batch";
            }
            node.strip_images = Boolean(node.strip_images);
          });
          graph = parsed;
          return;
        }
      } catch (error) {
        // Fall through to a fresh graph.
      }
    }
    graph = defaultGraph();
  }

  function slugName(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[\s_]+/g, "-")
      .replace(/[^a-z0-9-]+/g, "")
      .replace(/-{2,}/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  function uniqueName(base, ignoreIndex) {
    var name = slugName(base) || "node";
    var taken = {};
    graph.nodes.forEach(function (node, index) {
      if (index !== ignoreIndex && node.name) {
        taken[node.name] = true;
      }
    });
    if (!taken[name]) {
      return name;
    }
    var suffix = 2;
    while (taken[name + "-" + suffix]) {
      suffix += 1;
    }
    return name + "-" + suffix;
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "text") {
          node.textContent = attrs[key];
        } else if (key === "className") {
          node.className = attrs[key];
        } else if (key.indexOf("on") === 0) {
          node.addEventListener(key.slice(2), attrs[key]);
        } else {
          node.setAttribute(key, attrs[key]);
        }
      });
    }
    (children || []).forEach(function (child) {
      if (child) {
        node.appendChild(child);
      }
    });
    return node;
  }

  function labelField(text, control, hint) {
    var wrapper = el("label", {}, []);
    wrapper.appendChild(document.createTextNode(text));
    wrapper.appendChild(control);
    if (hint) {
      wrapper.appendChild(el("small", { text: hint }));
    }
    return wrapper;
  }

  function textInput(value, onInput, attrs) {
    var input = el("input", attrs || { type: "text" });
    input.value = value == null ? "" : value;
    input.addEventListener("input", function () {
      onInput(input.value);
    });
    return input;
  }

  function textarea(value, rows, onInput) {
    var box = el("textarea", { rows: String(rows) });
    box.value = value == null ? "" : value;
    box.addEventListener("input", function () {
      onInput(box.value);
    });
    return box;
  }

  function checkboxInput(checked, onChange) {
    var box = el("input", { type: "checkbox" });
    box.checked = Boolean(checked);
    box.addEventListener("change", function () {
      onChange(box.checked);
    });
    return box;
  }

  function sync() {
    hidden.value = JSON.stringify(graph);
  }

  function redraw() {
    sync();
    render();
    if (blocksModal && !blocksModal.hidden) {
      renderBlocksModal();
    }
    if (nodesModal && !nodesModal.hidden) {
      renderNodesModal();
    }
  }

  function moveItem(list, from, to) {
    if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) {
      return;
    }
    var item = list.splice(from, 1)[0];
    list.splice(to, 0, item);
  }

  function reorderButtons(onUp, onDown, onDelete) {
    var up = el("button", { type: "button", className: "wf-icon", text: "↑", title: "Move up", onclick: onUp });
    var down = el("button", { type: "button", className: "wf-icon", text: "↓", title: "Move down", onclick: onDown });
    var remove = el("button", { type: "button", className: "wf-icon wf-delete", text: "✕", title: "Delete", onclick: onDelete });
    return el("span", { className: "wf-actions" }, [up, down, remove]);
  }

  // -- drag & drop -----------------------------------------------------------

  function makeDroppable(card, acceptAt) {
    card.addEventListener("dragover", function (event) {
      if (!dragState) {
        return;
      }
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      card.classList.add(card.getBoundingClientRect().top + card.offsetHeight / 2 > event.clientY
        ? "wf-drop-before" : "wf-drop-after");
    });
    card.addEventListener("dragleave", function () {
      card.classList.remove("wf-drop-before", "wf-drop-after");
    });
    card.addEventListener("drop", function (event) {
      event.preventDefault();
      var before = card.classList.contains("wf-drop-before");
      card.classList.remove("wf-drop-before", "wf-drop-after");
      if (!dragState) {
        return;
      }
      var to = acceptAt(before);
      if (dragState.kind === "node") {
        moveItem(graph.nodes, dragState.index, to > dragState.index ? to - 1 : to);
      } else if (dragState.kind === "block" && card.classList.contains("wf-block")) {
        moveItem(graph.final.messages, dragState.index, to > dragState.index ? to - 1 : to);
      }
      dragState = null;
      redraw();
    });
  }

  function makeDraggable(card, kind, index) {
    card.setAttribute("draggable", "true");
    card.addEventListener("mousedown", function (event) {
      // A draggable ancestor swallows mouse carets inside inputs; drop the
      // flag whenever the press starts on an interactive control so textareas,
      // selects and buttons keep their normal mouse behaviour.
      var blocked = event.target && event.target.closest &&
        event.target.closest("input, textarea, select, button, label");
      card.setAttribute("draggable", blocked ? "false" : "true");
    });
    card.addEventListener("dragstart", function (event) {
      dragState = { kind: kind, index: index };
      event.dataTransfer.effectAllowed = "move";
      try {
        event.dataTransfer.setData("text/plain", kind + ":" + index);
      } catch (error) {
        // Some browsers refuse setData for arbitrary types.
      }
    });
    card.addEventListener("dragend", function () {
      dragState = null;
      document.querySelectorAll(".wf-drop-before, .wf-drop-after").forEach(function (node) {
        node.classList.remove("wf-drop-before", "wf-drop-after");
      });
    });
  }

  // -- cards -----------------------------------------------------------------

  function originCard() {
    var variables = ["core_context", "system_request"];
    if (graph.nodes.length) {
      variables.push("<node>.content (and <node>.<key> with Expect JSON)");
      variables.push("<node>.count for Image Captioner");
      if (graph.reasoning) {
        variables.push("<node>.reasoning");
      }
    }
    return el("article", { className: "wf-card wf-origin" }, [
      el("div", { className: "wf-card-header" }, [
        el("strong", { text: "Origin" }),
        el("span", { className: "pill", text: "fixed" })
      ]),
      el("p", { className: "wf-card-note", text: "Published to every node and the Final Request:" }),
      el("div", { className: "wf-vars" }, variables.map(function (name) {
        return el("code", { className: "wf-var", text: name });
      }))
    ]);
  }

  function nodeCard(node, index) {
    var nameInput = textInput(node.name, function (value) {
      node.name = slugName(value);
      sync();
    }, { type: "text", placeholder: "node-name" });
    nameInput.addEventListener("blur", function () {
      node.name = uniqueName(node.name || nameInput.value || "node", index);
      nameInput.value = node.name;
      sync();
    });

    var routePicker = el("select", {
      onchange: function (event) {
        node.route_id = event.target.value ? Number(event.target.value) : null;
        sync();
      }
    });
    routePicker.appendChild(el("option", { value: "", text: "— premodel default —" }));
    routes.forEach(function (route) {
      var option = el("option", { value: route.id, text: route.name });
      if (String(node.route_id || "") === String(route.id)) {
        option.setAttribute("selected", "selected");
      }
      routePicker.appendChild(option);
    });

    var modelInput = textInput(node.model, function (value) {
      node.model = value;
      sync();
    }, { type: "text", list: "premodel-models", placeholder: "premodel default" });

    var maxTokens = textInput(node.max_tokens == null ? "" : node.max_tokens, function (value) {
      var parsed = parseInt(value, 10);
      node.max_tokens = isNaN(parsed) || parsed < 1 ? null : parsed;
      sync();
    }, { type: "number", min: "1", step: "1", placeholder: "provider default" });

    var timeout = textInput(node.timeout_seconds == null ? 60 : node.timeout_seconds, function (value) {
      var parsed = parseInt(value, 10);
      node.timeout_seconds = isNaN(parsed) || parsed < 1 ? 60 : Math.min(parsed, 120);
      sync();
    }, { type: "number", min: "1", max: "120" });

    var typePicker = el("select", {
      onchange: function (event) {
        node.type = event.target.value;
        if (node.type === "image_captioner" && !node.image_mode) {
          node.image_mode = "batch";
        }
        sync();
        render();
      }
    });
    ["api_request", "image_captioner"].forEach(function (type) {
      var option = el("option", { value: type, text: NODE_TYPE_LABELS[type] });
      if ((node.type || "api_request") === type) {
        option.setAttribute("selected", "selected");
      }
      typePicker.appendChild(option);
    });

    var typeFields;
    if (node.type === "image_captioner") {
      var modePicker = el("select", {
        onchange: function (event) {
          node.image_mode = event.target.value === "individual" ? "individual" : "batch";
          sync();
        }
      });
      [["batch", "All images in one request (faster)"],
       ["individual", "One request per image (more compatible)"]].forEach(function (pair) {
        var option = el("option", { value: pair[0], text: pair[1] });
        if ((node.image_mode || "batch") === pair[0]) {
          option.setAttribute("selected", "selected");
        }
        modePicker.appendChild(option);
      });
      var promptsWrap = el("div", { className: "wf-prompts", hidden: "hidden" }, [
        labelField("System prompt (optional)", textarea(node.system_prompt, 2, function (value) {
          node.system_prompt = value;
          sync();
        })),
        labelField("Caption prompt", textarea(node.prompt, 3, function (value) {
          node.prompt = value;
          sync();
        }))
      ]);
      typeFields = [
        el("div", { className: "wf-grid" }, [
          labelField("Images", modePicker),
          labelField("Max tokens", maxTokens),
          labelField("Timeout (s)", timeout)
        ]),
        el("label", { className: "wf-inline" }, [
          checkboxInput(Boolean(node.strip_images), function (value) {
            node.strip_images = value;
            sync();
          }),
          document.createTextNode(
            " Strip images from the request after captioning (final forward + later nodes)"
          )
        ]),
        el("div", { className: "wf-prompts-toggle" }, [
          el("button", {
            type: "button",
            className: "secondary outline",
            text: "Prompts",
            onclick: function () {
              promptsWrap.hidden = !promptsWrap.hidden;
            }
          }),
          el("small", { text: "System prompt and caption prompt live here." })
        ]),
        promptsWrap
      ];
    } else {
      typeFields = [
        labelField("System prompt (optional)", textarea(node.system_prompt, 2, function (value) {
          node.system_prompt = value;
          sync();
        })),
        labelField("Prompt", textarea(node.prompt, 3, function (value) {
          node.prompt = value;
          sync();
        })),
        el("div", { className: "wf-grid" }, [
          el("label", { className: "wf-inline" }, [
            checkboxInput(node.expect_json, function (value) {
              node.expect_json = value;
              sync();
            }),
            document.createTextNode(" Expect JSON (keys become {{ " + (node.name || "node") + ".<key> }})")
          ]),
          labelField("Max tokens", maxTokens),
          labelField("Timeout (s)", timeout)
        ])
      ];
    }

    var body = el("div", { className: "wf-card-body" }, [
      labelField("Name", nameInput, "Variable prefix: {{ " + (node.name || "node") + ".content }}"),
      el("div", { className: "wf-grid" }, [
        labelField("Connection", routePicker),
        labelField("Model", modelInput)
      ])
    ].concat(typeFields));

    var card = el("article", { className: "wf-card wf-node" }, []);
    var collapseButton = el("button", {
      type: "button",
      className: "wf-icon",
      text: node.collapsed ? "▸" : "▾",
      title: node.collapsed ? "Expand node" : "Collapse node",
      onclick: function () {
        node.collapsed = !node.collapsed;
        redraw();
      }
    });
    card.appendChild(
      el("div", { className: "wf-card-header" }, [
        el("span", { className: "wf-drag", text: "⠿", title: "Drag to reorder" }),
        collapseButton,
        typePicker,
        reorderButtons(
          function () {
            moveItem(graph.nodes, index, index - 1);
            sync();
            render();
          },
          function () {
            moveItem(graph.nodes, index, index + 1);
            sync();
            render();
          },
          function () {
            graph.nodes.splice(index, 1);
            sync();
            render();
          }
        )
      ])
    );
    if (node.collapsed) {
      body.hidden = true;
      card.appendChild(
        el("p", {
          className: "wf-node-peek",
          text: (node.name || "node") + " · " + (node.model || "premodel default") +
            " — " + String(node.prompt || "").replace(/\s+/g, " ").slice(0, 90)
        })
      );
    }
    card.appendChild(body);
    makeDraggable(card, "node", index);
    makeDroppable(card, function (before) {
      return before ? index : index + 1;
    });
    return card;
  }

  function blockRow(block, index) {
    var controls;
    if (block.kind === "core_context") {
      controls = el("span", { className: "wf-block-kind", text: "Client messages (core context)" });
    } else {
      var rolePicker = el("select", {
        onchange: function (event) {
          block.role = event.target.value;
          sync();
        }
      });
      ["system", "user", "assistant"].forEach(function (role) {
        var option = el("option", { value: role, text: role });
        if ((block.role || "user") === role) {
          option.setAttribute("selected", "selected");
        }
        rolePicker.appendChild(option);
      });
      controls = el("div", { className: "wf-block-text" }, [
        rolePicker,
        textarea(block.content, 3, function (value) {
          block.content = value;
          sync();
        })
      ]);
    }
    var row = el("div", { className: "wf-card wf-block" }, [
      controls,
      reorderButtons(
        function () {
          moveItem(graph.final.messages, index, index - 1);
          redraw();
        },
        function () {
          moveItem(graph.final.messages, index, index + 1);
          redraw();
        },
        function () {
          if (graph.final.messages.length <= 1) {
            return;
          }
          graph.final.messages.splice(index, 1);
          redraw();
        }
      )
    ]);
    makeDraggable(row, "block", index);
    makeDroppable(row, function (before) {
      return before ? index : index + 1;
    });
    return row;
  }

  function blockSummary() {
    var textCount = 0;
    var coreCount = 0;
    graph.final.messages.forEach(function (block) {
      if (block.kind === "core_context") {
        coreCount += 1;
      } else {
        textCount += 1;
      }
    });
    var total = graph.final.messages.length;
    var parts = [];
    if (textCount) {
      parts.push(textCount + " text");
    }
    if (coreCount) {
      parts.push(coreCount + " core context");
    }
    return total + " block" + (total === 1 ? "" : "s") +
      (parts.length ? " (" + parts.join(", ") + ")" : "");
  }

  function addTextBlock() {
    graph.final.messages.push({ kind: "text", role: "system", content: "" });
    redraw();
  }

  function addCoreBlock() {
    graph.final.messages.push({ kind: "core_context" });
    redraw();
  }

  var blocksModal = null;

  function blocksModalContent() {
    var blocks = el("div", { className: "wf-blocks" });
    graph.final.messages.forEach(function (block, index) {
      blocks.appendChild(blockRow(block, index));
    });
    return el("div", { className: "wf-modal-panel" }, [
      el("div", { className: "wf-modal-header" }, [
        el("strong", { text: "Final Request blocks" }),
        el("button", {
          type: "button",
          className: "secondary outline",
          text: "✕",
          title: "Close",
          onclick: closeBlocksModal
        })
      ]),
      el("p", {
        className: "wf-card-note",
        text: "Text blocks are templated with every variable; core context blocks splice the client's original messages. Drag to reorder."
      }),
      blocks,
      el("div", { className: "wf-block-add" }, [
        el("button", {
          type: "button",
          text: "+ Add text block",
          onclick: addTextBlock
        }),
        el("button", {
          type: "button",
          className: "secondary outline",
          text: "+ Add core context",
          onclick: addCoreBlock
        })
      ])
    ]);
  }

  function renderBlocksModal() {
    if (!blocksModal) {
      return;
    }
    blocksModal.textContent = "";
    blocksModal.appendChild(blocksModalContent());
  }

  function openBlocksModal() {
    if (!blocksModal) {
      blocksModal = el("div", { className: "wf-modal", hidden: "hidden" });
      blocksModal.addEventListener("click", function (event) {
        if (event.target === blocksModal) {
          closeBlocksModal();
        }
      });
      document.body.appendChild(blocksModal);
    }
    renderBlocksModal();
    blocksModal.hidden = false;
  }

  function closeBlocksModal() {
    if (blocksModal) {
      blocksModal.hidden = true;
    }
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeBlocksModal();
      closeNodesModal();
    }
  });

  function finalCard() {
    var override = textInput(graph.final.model_override, function (value) {
      graph.final.model_override = value.trim();
      sync();
    }, { type: "text", placeholder: "premodel default" });

    var card = el("article", { className: "wf-card wf-final" }, [
      el("div", { className: "wf-card-header" }, [
        el("strong", { text: "Final Request" }),
        el("span", { className: "pill", text: "fixed" })
      ]),
      el("p", {
        className: "wf-card-note",
        text: "The message list forwarded upstream."
      }),
      el("div", { className: "wf-block-summary" }, [
        el("span", { className: "wf-block-count", text: blockSummary() }),
        el("button", {
          type: "button",
          className: "secondary outline",
          text: "Edit blocks",
          onclick: openBlocksModal
        })
      ]),
      labelField("Model override (optional)", override)
    ]);
    makeDroppable(card, function () {
      return graph.nodes.length;
    });
    return card;
  }

  function addNodeButton() {
    return el("div", { className: "wf-add-node" }, [
      el("button", {
        type: "button",
        text: "+ Add node",
        onclick: openNodesModal
      })
    ]);
  }

  var nodesModal = null;

  function addNode(type) {
    var node = cloneDefaults(type);
    var base = type === "image_captioner" ? "captioner-" : "node-";
    node.name = uniqueName(base + (graph.nodes.length + 1));
    graph.nodes.push(node);
    closeNodesModal();
    redraw();
  }

  function nodeOption(type, title, description) {
    return el("button", {
      type: "button",
      className: "wf-node-option",
      onclick: function () {
        addNode(type);
      }
    }, [
      el("strong", { text: title }),
      el("small", { text: description })
    ]);
  }

  function nodesModalContent() {
    return el("div", { className: "wf-modal-panel" }, [
      el("div", { className: "wf-modal-header" }, [
        el("strong", { text: "Add node" }),
        el("button", {
          type: "button",
          className: "secondary outline",
          text: "✕",
          title: "Close",
          onclick: closeNodesModal
        })
      ]),
      el("p", {
        className: "wf-card-note",
        text: "Nodes run in order between Origin and Final Request. Pick a type to insert."
      }),
      nodeOption(
        "api_request",
        "API Request",
        "Send a prompt to a model and use the reply — raw text, or parsed keys with Expect JSON."
      ),
      nodeOption(
        "image_captioner",
        "Image Captioner",
        "Caption every image in the caller's messages with a vision model (batch or one-by-one)."
      )
    ]);
  }

  function renderNodesModal() {
    if (!nodesModal) {
      return;
    }
    nodesModal.textContent = "";
    nodesModal.appendChild(nodesModalContent());
  }

  function openNodesModal() {
    if (!nodesModal) {
      nodesModal = el("div", { className: "wf-modal", hidden: "hidden" });
      nodesModal.addEventListener("click", function (event) {
        if (event.target === nodesModal) {
          closeNodesModal();
        }
      });
      document.body.appendChild(nodesModal);
    }
    renderNodesModal();
    nodesModal.hidden = false;
  }

  function closeNodesModal() {
    if (nodesModal) {
      nodesModal.hidden = true;
    }
  }

  function reasoningSwitchCard() {
    return el("article", { className: "wf-card wf-reasoning" }, [
      el("div", { className: "wf-card-header" }, [
        el("strong", { text: "Node reasoning" }),
        el("span", { className: "pill", text: "optional" })
      ]),
      el("p", {
        className: "wf-card-note",
        text: "When a node's reply carries reasoning, prefix it to the reasoning the client receives, one logged block per node."
      }),
      el("label", { className: "wf-inline" }, [
        checkboxInput(Boolean(graph.reasoning), function (value) {
          graph.reasoning = value;
          sync();
          render();
        }),
        document.createTextNode(" Surface node reasoning in responses")
      ])
    ]);
  }

  function render() {
    canvas.textContent = "";
    canvas.appendChild(originCard());
    canvas.appendChild(reasoningSwitchCard());
    graph.nodes.forEach(function (node, index) {
      canvas.appendChild(nodeCard(node, index));
    });
    canvas.appendChild(addNodeButton());
    canvas.appendChild(finalCard());
  }

  function updatePanels() {
    var on = checkbox.checked;
    editor.hidden = !on;
    if (promptSections) {
      promptSections.hidden = on;
    }
  }

  checkbox.addEventListener("change", function () {
    updatePanels();
  });
  loadGraph();
  sync();
  render();
  updatePanels();
})();
