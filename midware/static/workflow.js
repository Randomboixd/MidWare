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
    timeout_seconds: 60,
    use_system_timezone: true,
    trim_count: 0,
    trim_drop_system: false,
    trim_drop_images: false,
    allow_network: false
  };
  var NODE_TYPE_LABELS = {
    api_request: "API Request",
    image_captioner: "Image Captioner",
    environment: "Environment",
    context: "Context",
    assure: "Assure",
    javascript: "JavaScript"
  };
  var REQUEST_TYPES = { api_request: true, image_captioner: true };
  var ENV_ACTION_OPTIONS = [
    ["time", "Declare Time"]
  ];
  var ENV_ACTION_LABELS_MAP = {};
  ENV_ACTION_OPTIONS.forEach(function (pair) {
    ENV_ACTION_LABELS_MAP[pair[0]] = pair[1];
  });
  var ASSURE_OPERATOR_OPTIONS = [
    ["equals", "Equals"],
    ["not_equals", "Not equals"],
    ["contains", "Contains"],
    ["not_contains", "Does not contain"],
    ["starts_with", "Starts with"],
    ["ends_with", "Ends with"],
    ["greater", "Greater than"],
    ["less", "Less than"],
    ["empty", "Is empty"],
    ["not_empty", "Is not empty"]
  ];
  var ASSURE_UNARY = { empty: true, not_empty: true };
  var dragState = null;
  var graph = defaultGraph();
  var JS_SCRIPT_STARTER = "async function run(midware) {\n  return \"\";\n}";

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
    if (node.type === "environment") {
      node.actions = ["time"];
    }
    if (node.type === "assure") {
      node.left = "";
      node.right = "";
      node.operator = "equals";
    }
    if (node.type === "javascript") {
      // The shared default (60) exceeds the JS node's 30s cap, which would make
      // a freshly created card invalid until retyped.
      node.timeout_seconds = 5;
      if (!node.prompt) {
        node.prompt = JS_SCRIPT_STARTER;
      }
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
            node.use_system_timezone = node.use_system_timezone !== false;
            node.trim_drop_system = Boolean(node.trim_drop_system);
            node.trim_drop_images = Boolean(node.trim_drop_images);
            node.allow_network = Boolean(node.allow_network);
            if (!(typeof node.trim_count === "number" && node.trim_count >= 0)) {
              node.trim_count = 0;
            }
            if (!Array.isArray(node.actions)) {
              node.actions = node.type === "environment" ? ["time"] : [];
            }
            if (typeof node.left !== "string") {
              node.left = "";
            }
            if (typeof node.right !== "string") {
              node.right = "";
            }
            var knownOperator = ASSURE_OPERATOR_OPTIONS.some(function (pair) {
              return pair[0] === node.operator;
            });
            if (!knownOperator) {
              node.operator = "equals";
            }
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

  // -- CodeMirror (lazy, optional) --------------------------------------------
  // Loaded from esm.sh on first JS node render; every package pins the same
  // dependency versions via ?deps= so esm.sh cannot split @codemirror/state
  // into two instances (which surfaces as "Unrecognized extension value").
  // Any import or mount failure keeps the plain textarea — never blocks save.

  var CM_VERSIONS = {
    "codemirror": "6.0.2",
    "@codemirror/state": "6.7.6",
    "@codemirror/view": "6.43.14",
    "@codemirror/language": "6.13.1",
    "@codemirror/autocomplete": "6.20.3",
    "@codemirror/commands": "6.11.1",
    "@codemirror/search": "6.7.2",
    "@codemirror/lint": "6.9.7",
    "@codemirror/lang-javascript": "6.2.5",
    "@lezer/common": "1.5.3",
    "@lezer/highlight": "1.2.5",
    "@lezer/lr": "1.4.11",
    "@lezer/javascript": "1.5.6"
  };
  var cmLoadPromise = null;

  function cmUrl(name) {
    // No ?deps= / ?shared= here on purpose: those make esm.sh emit custom
    // build URLs (…/X-Z…/) that are a different module path than the plain
    // redirects the meta-package imports internally, so the browser ends up
    // with two @codemirror/state instances and EditorState.create throws
    // "Unrecognized extension value". Plain pinned URLs all converge on the
    // same final …/es2022/*.mjs, which is what we want.
    return "https://esm.sh/" + name + "@" + CM_VERSIONS[name] + "?target=es2022";
  }

  function loadCodeMirror() {
    if (!cmLoadPromise) {
      // The `codemirror` meta-package only re-exports basicSetup/minimalSetup —
      // EditorView, EditorState and tags must come from their real packages.
      cmLoadPromise = Promise.all([
        import(cmUrl("codemirror")),
        import(cmUrl("@codemirror/view")),
        import(cmUrl("@codemirror/state")),
        import(cmUrl("@codemirror/lang-javascript")),
        import(cmUrl("@codemirror/autocomplete")),
        import(cmUrl("@codemirror/language")),
        import(cmUrl("@lezer/highlight"))
      ]).then(function (m) {
        return {
          basicSetup: m[0].basicSetup,
          EditorView: m[1].EditorView,
          EditorState: m[2].EditorState,
          javascript: m[3].javascript,
          snippetCompletion: m[4].snippetCompletion,
          HighlightStyle: m[5].HighlightStyle,
          syntaxHighlighting: m[5].syntaxHighlighting,
          tags: m[6].tags
        };
      }).catch(function (error) {
        // Allow a retry on the next script box instead of disabling the editor
        // for the rest of the page session after one transient CDN failure.
        cmLoadPromise = null;
        throw error;
      });
    }
    return cmLoadPromise;
  }

  // Mirrors the sandbox globals probe (test_workflows_js.py
  // test_js_sandbox_globals_match_probe): everything listed as present exists
  // in the mini-racer isolate; the ABSENT list is offered with an info tooltip
  // so nobody wastes time on URL/fetch.
  var JS_KEYWORD_OPTIONS = [
    "async", "await", "break", "case", "catch", "class", "const", "continue",
    "default", "delete", "do", "else", "export", "extends", "finally", "for",
    "function", "if", "import", "in", "instanceof", "let", "new", "null",
    "return", "super", "switch", "this", "throw", "try", "typeof", "undefined",
    "var", "void", "while", "true", "false"
  ].map(function (word) {
    return { label: word, type: "keyword" };
  });

  var JS_SANDBOX_GLOBAL_OPTIONS = [
    "Math", "JSON", "Object", "Array", "String", "Number", "Boolean", "Date",
    "RegExp", "Promise", "Map", "Set", "WeakMap", "WeakSet", "Symbol", "BigInt",
    "Error", "TypeError", "RangeError", "SyntaxError", "ReferenceError",
    "EvalError", "URIError", "AggregateError",
    "Intl", "Proxy", "Reflect", "WeakRef", "FinalizationRegistry",
    "ArrayBuffer", "SharedArrayBuffer", "DataView",
    "Int8Array", "Uint8Array", "Uint8ClampedArray",
    "Int16Array", "Uint16Array", "Int32Array", "Uint32Array",
    "Float32Array", "Float64Array", "BigInt64Array", "BigUint64Array",
    "isNaN", "isFinite", "parseInt", "parseFloat",
    "encodeURIComponent", "decodeURIComponent", "escape", "unescape",
    "setTimeout", "clearTimeout"
  ].map(function (name) {
    return { label: name, type: "variable" };
  });

  var JS_ABSENT_API_OPTIONS = [
    "URL", "URLSearchParams", "TextEncoder", "TextDecoder",
    "atob", "btoa", "structuredClone", "crypto",
    "setInterval", "clearInterval", "queueMicrotask",
    "AbortController", "AbortSignal", "fetch"
  ].map(function (name) {
    return {
      label: name,
      type: "variable",
      info: "Not available in this sandbox."
    };
  });

  var JS_KEYWORDS = {};
  JS_KEYWORD_OPTIONS.forEach(function (opt) {
    JS_KEYWORDS[opt.label] = true;
  });

  function regexEscape(text) {
    return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function isApiRequestInstance(doc, name) {
    // Collect class names that extend APIRequest, then variables assigned
    // from `new <those classes>(…)`. Covers `const g = new GemmaRequest()`
    // without needing full JS analysis.
    var classes = {};
    var classRe = /\bclass\s+([A-Za-z_$][\w$]*)\s+extends\s+APIRequest\b/g;
    var m;
    while ((m = classRe.exec(doc)) !== null) {
      classes[m[1]] = true;
    }
    if (!Object.keys(classes).length) {
      return false;
    }
    var newRe = new RegExp(
      "\\b(?:const|let|var)?\\s*" + regexEscape(name) + "\\s*=\\s*new\\s+([A-Za-z_$][\\w$]*)\\s*\\(",
      "g"
    );
    while ((m = newRe.exec(doc)) !== null) {
      if (classes[m[1]]) {
        return true;
      }
    }
    return false;
  }

  function isApiImageInstance(doc, name) {
    // Mirror of isApiRequestInstance for the image-generation base class.
    var classes = {};
    var classRe = /\bclass\s+([A-Za-z_$][\w$]*)\s+extends\s+APIImageRequest\b/g;
    var m;
    while ((m = classRe.exec(doc)) !== null) {
      classes[m[1]] = true;
    }
    if (!Object.keys(classes).length) {
      return false;
    }
    var newRe = new RegExp(
      "\\b(?:const|let|var)?\\s*" + regexEscape(name) + "\\s*=\\s*new\\s+([A-Za-z_$][\\w$]*)\\s*\\(",
      "g"
    );
    while ((m = newRe.exec(doc)) !== null) {
      if (classes[m[1]]) {
        return true;
      }
    }
    return false;
  }

  function classExtends(doc, cls) {
    var m = new RegExp(
      "\\bclass\\s+" + regexEscape(cls) + "\\s+extends\\s+([A-Za-z_$][\\w$]*)\\b"
    ).exec(doc);
    return m ? m[1] : null;
  }

  // The class behind a `.create(…)`-style receiver: `new X(…)` itself, or a
  // variable assigned from `new X(…)`.
  function instanceClass(doc, expr) {
    var m = /^\s*new\s+([A-Za-z_$][\w$]*)\s*\(/.exec(expr);
    if (m) { return m[1]; }
    if (!/^\s*[A-Za-z_$][\w$]*\s*$/.test(expr)) { return null; }
    var id = expr.replace(/^\s+|\s+$/g, "");
    m = new RegExp(
      "\\b(?:const|let|var)?\\s*" + regexEscape(id) + "\\s*=\\s*new\\s+([A-Za-z_$][\\w$]*)\\s*\\("
    ).exec(doc);
    return m ? m[1] : null;
  }

  function isImageReceiver(doc, expr) {
    var cls = instanceClass(doc, expr);
    if (!cls) { return false; }
    return cls === "APIImageRequest" || classExtends(doc, cls) === "APIImageRequest";
  }

  function isImageCall(doc, before) {
    // The call whose "(" sits at the end of `before` — the last match wins.
    var re = /(new\s+[\w$]+\s*\([^)]*\)|[\w$.]+)\.(?:create|chat|ask|fetch)\s*\(/g;
    var m, last = null;
    while ((m = re.exec(before)) !== null) { last = m; }
    return last ? isImageReceiver(doc, last[1]) : false;
  }

  function thisInstanceOptions(doc) {
    // `this.` inside a script that only defines image subclasses completes
    // image members; mixed or plain scripts keep the chat members.
    if (/\bextends\s+APIImageRequest\b/.test(doc) && !/\bextends\s+APIRequest\b/.test(doc)) {
      return API_IMAGE_REQUEST_OPTIONS;
    }
    return API_REQUEST_OPTIONS;
  }

  function isImageResponseVar(doc, name) {
    // `const r = await img.create(…)` where img is an APIImageRequest instance.
    // Checked before isChatResponseVar, whose looser pattern also matches this.
    var re = new RegExp(
      "\\b(?:const|let|var)?\\s*" + regexEscape(name) +
        "\\s*=\\s*await\\s+(new\\s+[\\w$]+\\s*\\([^)]*\\)|[\\w$.]+)\\.create\\s*\\(",
      "g"
    );
    var m;
    while ((m = re.exec(doc)) !== null) {
      if (isImageReceiver(doc, m[1])) { return true; }
    }
    return false;
  }

  function isChatResponseVar(doc, name) {
    // `const resp = await x.create(…)` / `await midware.chat(…)` / even
    // `await new R().create(…)` → the variable holds the full OpenAI-style
    // response object. `.ask(…)` returns a plain string, so it is excluded.
    var re = new RegExp(
      "\\b(?:const|let|var)?\\s*" + regexEscape(name) +
        "\\s*=\\s*await\\s+(?:new\\s+[\\w$]+\\s*\\([^)]*\\)|[\\w$.]+)\\.(?:create|chat)\\s*\\(",
      "g"
    );
    return re.test(doc);
  }

  function isFetchResponseVar(doc, name) {
    // `const r = await m.fetch(…)` → {status, ok, body, headers}.
    var re = new RegExp(
      "\\b(?:const|let|var)?\\s*" + regexEscape(name) +
        "\\s*=\\s*await\\s+[\\w$.]+\\.fetch\\s*\\(",
      "g"
    );
    return re.test(doc);
  }

  // Walks text before the cursor, skipping strings/comments, and finds the
  // innermost unclosed "{" with enough context to classify it. The stack also
  // records, per object brace, the offset just past its last top-level comma
  // so we can tell a key position from a value position even after nested
  // arrays/objects (e.g. chat({ messages: [{…}], <here> })).
  function scanObjectContext(text) {
    var stack = [];
    var stringDelim = null;
    var escaped = false;
    var inLineComment = false;
    var inBlockComment = false;
    var i = 0;
    var n = text.length;
    while (i < n) {
      var ch = text[i];
      if (inLineComment) {
        if (ch === "\n") { inLineComment = false; }
        i++; continue;
      }
      if (inBlockComment) {
        if (ch === "*" && text[i + 1] === "/") { inBlockComment = false; i += 2; continue; }
        i++; continue;
      }
      if (stringDelim) {
        if (escaped) { escaped = false; i++; continue; }
        if (ch === "\\") { escaped = true; i++; continue; }
        if (ch === stringDelim) { stringDelim = null; }
        i++; continue;
      }
      if (ch === "/" && text[i + 1] === "/") { inLineComment = true; i += 2; continue; }
      if (ch === "/" && text[i + 1] === "*") { inBlockComment = true; i += 2; continue; }
      if (ch === "\"" || ch === "'" || ch === "`") { stringDelim = ch; i++; continue; }
      if (ch === "(" || ch === "[") { stack.push({ ch: ch, idx: i }); i++; continue; }
      if (ch === "{") {
        stack.push({ ch: ch, idx: i, keyStart: i + 1 });
        i++; continue;
      }
      if (ch === ")" || ch === "]" || ch === "}") {
        stack.pop();
        i++; continue;
      }
      if (ch === "," && stack.length && stack[stack.length - 1].ch === "{") {
        // A comma directly inside an object starts a new key slot.
        stack[stack.length - 1].keyStart = i + 1;
      }
      i++;
    }
    if (!stack.length) { return null; }
    var top = stack[stack.length - 1];
    if (top.ch !== "{") { return null; }
    var below = stack.length >= 2 ? stack[stack.length - 2] : null;
    var insideMessages = Boolean(
      below && below.ch === "[" && /messages\s*:\s*$/.test(text.slice(0, below.idx))
    );
    return {
      before: text.slice(0, top.idx),
      tail: text.slice(top.keyStart),
      insideMessages: insideMessages
    };
  }

  // Classifies the innermost open object literal so we know which key set to
  // offer: chat params, image params, fetch opts, or a chat message's
  // {role, content}.
  function objectKeyOptionsFor(context, doc) {
    if (context.insideMessages) { return "message"; }
    var before = context.before;
    if (/\.(?:create|chat)\s*\(\s*$/.test(before)) {
      return isImageCall(doc, before) ? "image" : "chat";
    }
    if (/\.(?:ask|fetch)\s*\([^()]*,\s*$/.test(before)) {
      // ask(x, {…}) → param object (chat or image per the receiver);
      // fetch(url, {…}) → fetch opts.
      if (/\.fetch\s*\([^()]*,\s*$/.test(before)) { return "fetch"; }
      return isImageCall(doc, before) ? "imageAsk" : "chatAsk";
    }
    return null;
  }

  // Pulls the full member chain (e.g. "resp.choices[0].message") plus the
  // partial identifier typed after the final dot, walking back over balanced
  // brackets. Returns null when the cursor is not after a dot.
  function extractMemberChain(textBefore) {
    var i = textBefore.length;
    while (i > 0 && /[\w$]/.test(textBefore[i - 1])) { i--; }
    var partial = textBefore.slice(i);
    var j = i;
    while (j > 0 && /\s/.test(textBefore[j - 1])) { j--; }
    if (j === 0 || textBefore[j - 1] !== ".") { return null; }
    j--;
    var depth = 0;
    var k = j;
    while (k > 0) {
      var ch = textBefore[k - 1];
      if (ch === "]") { depth++; k--; continue; }
      if (ch === "[") { depth--; k--; continue; }
      if (depth > 0) { k--; continue; }
      if (/[\w$.]/.test(ch)) { k--; continue; }
      break;
    }
    var chain = textBefore.slice(k, j).replace(/^\s+|\s+$/g, "");
    if (!chain || !/^[A-Za-z_$]/.test(chain)) { return null; }
    return { chain: chain, partial: partial };
  }

  // Option sets shared by the completion source. Kept at module scope so the
  // chain resolver (also module scope) can reference them.
  var MW_OPTIONS = [
    { label: "messages", type: "property", detail: "caller conversation", info: "Array of {role, content} messages from the client's request." },
    { label: "vars", type: "property", detail: "earlier variables", info: "Variables published by earlier nodes (and this node so far)." },
    { label: "systemRequest", type: "property", detail: "original request", info: "JSON string: {method, path, headers minus credentials}." },
    { label: "set", type: "method", detail: "(key, value)", info: "Publish {{ <node-name>.key }} for later nodes and the Final Request." },
    { label: "setMessages", type: "method", detail: "(array)", info: "Rewrite the conversation context for later nodes and the final forward." },
    { label: "fetch", type: "method", detail: "await (url, opts)", info: "HTTP(S) only, 10s timeout, 1MB cap. Requires Allow network. Returns {status, ok, body, headers}." },
    { label: "chat", type: "method", detail: "await ({messages, model, ...})", info: "Server-side chat through this node's connection. Usage folds into the request row. Does NOT require Allow network." }
  ];
  var API_REQUEST_OPTIONS = [
    { label: "model", type: "property", detail: "default model for create/ask" },
    { label: "temperature", type: "property", detail: "sampling temperature" },
    { label: "max_tokens", type: "property", detail: "completion budget" },
    { label: "create", type: "method", detail: "await (params)", info: "Returns the full OpenAI-style response object — see resp.choices / resp.usage below." },
    { label: "ask", type: "method", detail: "await (text, params)", info: "Sends one user message; returns just the assistant's reply as a string." }
  ];
  var API_IMAGE_REQUEST_OPTIONS = [
    { label: "model", type: "property", detail: "default model for create/ask" },
    { label: "size", type: "property", detail: "e.g. \"1024x1024\"" },
    { label: "n", type: "number", detail: "how many images" },
    { label: "quality", type: "property", detail: "\"low\" | \"medium\" | \"high\"" },
    { label: "response_format", type: "property", detail: "\"url\" | \"b64_json\"" },
    { label: "create", type: "method", detail: "await (params)", info: "Returns the full image response — see resp.data / resp.usage below." },
    { label: "ask", type: "method", detail: "await (prompt, params)", info: "Generates images from the prompt; returns the first image's url (or b64_json) as a string." }
  ];
  var CHAT_PARAM_OPTIONS = [
    { label: "messages", type: "property", detail: "[{role, content}]", info: "Required. The conversation to send." },
    { label: "model", type: "property", detail: "model id", info: "Falls back to the node's/default model if omitted." },
    { label: "temperature", type: "number", detail: "0–2" },
    { label: "top_p", type: "number", detail: "nucleus sampling" },
    { label: "max_tokens", type: "number", detail: "completion budget" },
    { label: "max_completion_tokens", type: "number", detail: "newer alias for max_tokens" },
    { label: "stop", type: "property", detail: "string or array" },
    { label: "presence_penalty", type: "number", detail: "-2–2" },
    { label: "frequency_penalty", type: "number", detail: "-2–2" },
    { label: "seed", type: "number", detail: "reproducibility" },
    { label: "logit_bias", type: "property", detail: "{token_id: bias}" },
    { label: "user", type: "string", detail: "end-user identifier" },
    { label: "response_format", type: "property", detail: "{type: \"json_object\"}" },
    { label: "tools", type: "property", detail: "function-calling tools" },
    { label: "tool_choice", type: "property", detail: "\"auto\" | \"none\" | object" }
  ];
  var MESSAGE_OBJECT_OPTIONS = [
    { label: "role", type: "property", detail: "\"system\" | \"user\" | \"assistant\"" },
    { label: "content", type: "property", detail: "string or array", info: "The message text. For vision, an array like [{type:\"image_url\", image_url:{url}}]." }
  ];
  var IMAGE_PARAM_OPTIONS = [
    { label: "prompt", type: "string", detail: "required", info: "What to render." },
    { label: "model", type: "property", detail: "model id", info: "Falls back to the node's model if omitted." },
    { label: "n", type: "number", detail: "how many images" },
    { label: "size", type: "string", detail: "\"1024x1024\"" },
    { label: "quality", type: "string", detail: "\"low\" | \"medium\" | \"high\"" },
    { label: "response_format", type: "string", detail: "\"url\" | \"b64_json\"" },
    { label: "style", type: "string", detail: "\"vivid\" | \"natural\"" },
    { label: "background", type: "string", detail: "\"transparent\" | \"opaque\"" },
    { label: "output_format", type: "string", detail: "\"png\" | \"jpeg\" | \"webp\"" },
    { label: "seed", type: "number", detail: "reproducibility" },
    { label: "user", type: "string", detail: "end-user identifier" }
  ];
  var CHAT_RESPONSE_OPTIONS = [
    { label: "choices", type: "property", detail: "array", info: "One entry per choice. choices[0].message.content is the assistant's reply." },
    { label: "choices[0].message", type: "property", detail: "{role, content}", info: "The assistant message. content is the reply text; reasoning_content may also be present on reasoning models." },
    { label: "choices[0].message.content", type: "string", detail: "assistant reply text" },
    { label: "choices[0].finish_reason", type: "string", detail: "\"stop\", \"length\", …" },
    { label: "usage", type: "property", detail: "{prompt_tokens, completion_tokens, total_tokens}" },
    { label: "usage.prompt_tokens", type: "number", detail: "input tokens" },
    { label: "usage.completion_tokens", type: "number", detail: "output tokens" },
    { label: "usage.total_tokens", type: "number", detail: "prompt + completion" },
    { label: "id", type: "string", detail: "response id" },
    { label: "model", type: "string", detail: "model that served the request" },
    { label: "created", type: "number", detail: "unix timestamp" }
  ];
  var CHOICE_OPTIONS = [
    { label: "index", type: "number", detail: "choice index" },
    { label: "message", type: "property", detail: "{role, content}" },
    { label: "finish_reason", type: "string", detail: "\"stop\", \"length\", …" },
    { label: "logprobs", type: "property", detail: "logprobs object" }
  ];
  var MESSAGE_OPTIONS = [
    { label: "role", type: "string", detail: "\"assistant\"" },
    { label: "content", type: "string", detail: "assistant reply text" },
    { label: "reasoning_content", type: "string", detail: "reasoning text (reasoning models)" }
  ];
  var USAGE_OPTIONS = [
    { label: "prompt_tokens", type: "number", detail: "input tokens" },
    { label: "completion_tokens", type: "number", detail: "output tokens" },
    { label: "total_tokens", type: "number", detail: "prompt + completion" }
  ];
  var IMAGE_RESPONSE_OPTIONS = [
    { label: "data", type: "property", detail: "array", info: "One entry per image: {url or b64_json, revised_prompt}." },
    { label: "created", type: "number", detail: "unix timestamp" },
    { label: "quality", type: "string", detail: "quality actually used" },
    { label: "size", type: "string", detail: "size actually used" },
    { label: "usage", type: "property", detail: "{input_tokens, output_tokens}" }
  ];
  var IMAGE_ITEM_OPTIONS = [
    { label: "url", type: "string", detail: "temporary image URL" },
    { label: "b64_json", type: "string", detail: "base64 image when response_format is b64_json" },
    { label: "revised_prompt", type: "string", detail: "provider's rewritten prompt" }
  ];
  var IMAGE_USAGE_OPTIONS = [
    { label: "input_tokens", type: "number", detail: "prompt tokens" },
    { label: "output_tokens", type: "number", detail: "image tokens" },
    { label: "prompt_tokens", type: "number", detail: "alias on some providers" },
    { label: "completion_tokens", type: "number", detail: "alias on some providers" }
  ];
  var FETCH_RESPONSE_OPTIONS = [
    { label: "status", type: "number", detail: "HTTP status code" },
    { label: "ok", type: "boolean", detail: "2xx" },
    { label: "body", type: "string", detail: "raw text (utf-8, truncated at 1 MB)" },
    { label: "headers", type: "property", detail: "response headers" }
  ];
  var FETCH_OPTS_OPTIONS = [
    { label: "method", type: "property", detail: "\"GET\", \"POST\", …", info: "Defaults to GET." },
    { label: "headers", type: "property", detail: "{name: value}" },
    { label: "body", type: "string", detail: "request body (string only)" }
  ];
  var CONSOLE_OPTIONS = [
    { label: "log", type: "method", detail: "(...args)" },
    { label: "error", type: "method", detail: "(...args)" },
    { label: "warn", type: "method", detail: "(...args)" }
  ];

  // Resolves a member chain to the option set for its last segment, so
  // resp.choices[0].message. completes message keys and midware.messages[0].
  // completes message keys too.
  function resolveChainOptions(doc, chain) {
    var rootMatch = /^([A-Za-z_$][\w$]*)/.exec(chain);
    if (!rootMatch) { return null; }
    var root = rootMatch[1];
    var rest = chain.slice(root.length);
    if (isFetchResponseVar(doc, root)) {
      return rest === "" ? FETCH_RESPONSE_OPTIONS : null;
    }
    if (isImageResponseVar(doc, root)) {
      if (rest === "") { return IMAGE_RESPONSE_OPTIONS; }
      if (/^\.data$/.test(rest)) { return null; }
      if (/^\.data\[\d+\]$/.test(rest)) { return IMAGE_ITEM_OPTIONS; }
      if (/^\.usage$/.test(rest)) { return IMAGE_USAGE_OPTIONS; }
      return null;
    }
    if (isChatResponseVar(doc, root)) {
      if (rest === "") { return CHAT_RESPONSE_OPTIONS; }
      if (/^\.choices$/.test(rest)) { return null; }
      if (/^\.choices\[\d+\]$/.test(rest)) { return CHOICE_OPTIONS; }
      if (/^\.choices\[\d+\]\.message$/.test(rest)) { return MESSAGE_OPTIONS; }
      if (/^\.usage$/.test(rest)) { return USAGE_OPTIONS; }
      return null;
    }
    if (root === "midware") {
      if (rest === "") { return MW_OPTIONS; }
      if (/^\.messages\[\d+\]$/.test(rest)) { return MESSAGE_OBJECT_OPTIONS; }
      return null;
    }
    if (root === "console") {
      return rest === "" ? CONSOLE_OPTIONS : null;
    }
    if (root === "this") {
      return rest === "" ? thisInstanceOptions(doc) : null;
    }
    if (isApiImageInstance(doc, root)) {
      return rest === "" ? API_IMAGE_REQUEST_OPTIONS : null;
    }
    if (isApiRequestInstance(doc, root)) {
      return rest === "" ? API_REQUEST_OPTIONS : null;
    }
    return null;
  }

  function makeJsCompletionSource(cm) {
    var runSnippet = cm.snippetCompletion(
      "async function run(midware) {\n  ${}\n}",
      {
        label: "async function run(midware)",
        type: "function",
        detail: "required entry point",
        info: "The workflow calls this with the midware bridge. The return value becomes the node's content."
      }
    );
    var apiRequestSnippet = cm.snippetCompletion(
      "class ${Name} extends APIRequest {\n  constructor() {\n    super();\n    this.model = \"${model}\";\n  }\n}",
      {
        label: "class X extends APIRequest",
        type: "class",
        detail: "subclass skeleton",
        info: "create(params) and ask(text) use this.model / this.temperature / this.max_tokens."
      }
    );
    var apiImageRequestSnippet = cm.snippetCompletion(
      "class ${Name} extends APIImageRequest {\n  constructor() {\n    super();\n    this.model = \"${model}\";\n  }\n}",
      {
        label: "class X extends APIImageRequest",
        type: "class",
        detail: "image subclass skeleton",
        info: "create(params) returns the full image response; ask(prompt) returns the first image's url (or b64_json)."
      }
    );
    var topLevel = [
      runSnippet,
      apiRequestSnippet,
      apiImageRequestSnippet,
      { label: "midware", type: "variable", detail: "sandbox bridge", info: "messages, vars, systemRequest, set, setMessages, fetch, chat." },
      { label: "APIRequest", type: "class", detail: "model-call base class", info: "Subclass it and use create/ask, or just await midware.chat(...)." },
      { label: "APIImageRequest", type: "class", detail: "image-generation base class", info: "Subclass it and use create({prompt, ...}) / ask(prompt) to generate images through the connection." },
      { label: "console", type: "variable", detail: "log → node reasoning" }
    ].concat(JS_KEYWORD_OPTIONS, JS_SANDBOX_GLOBAL_OPTIONS, JS_ABSENT_API_OPTIONS);

    function optionsFor(set, partial) {
      var lower = partial.toLowerCase();
      return {
        options: set.filter(function (opt) {
          return !partial || opt.label.toLowerCase().indexOf(lower) === 0;
        })
      };
    }

    return function (context) {
      var pos = context.pos;
      var doc = context.state.doc.toString();
      var textBefore = context.state.sliceDoc(Math.max(0, pos - 400), pos);

      // Object-literal keys: the string/comment-aware scanner finds the
      // innermost unclosed "{" and whether we sit at a key slot inside it.
      var objectContext = scanObjectContext(textBefore);
      if (objectContext) {
        var kind = objectKeyOptionsFor(objectContext, doc);
        if (kind && /^[\s]*[\w$]*$/.test(objectContext.tail)) {
          var set = null;
          if (kind === "message") { set = MESSAGE_OBJECT_OPTIONS; }
          else if (kind === "chat" || kind === "chatAsk") { set = CHAT_PARAM_OPTIONS; }
          else if (kind === "image" || kind === "imageAsk") { set = IMAGE_PARAM_OPTIONS; }
          else if (kind === "fetch") { set = FETCH_OPTS_OPTIONS; }
          if (set) {
            var partialKey = /[\w$]*$/.exec(objectContext.tail)[0];
            var result = optionsFor(set, partialKey);
            result.from = pos - partialKey.length;
            return result;
          }
        }
      }

      // Member chains: resp.choices[0].message. → message keys, etc.
      var chain = extractMemberChain(textBefore);
      if (chain) {
        var members = resolveChainOptions(doc, chain.chain);
        if (members) {
          var chainResult = optionsFor(members, chain.partial);
          chainResult.from = pos - chain.partial.length;
          return chainResult;
        }
        return null;
      }

      var word = context.matchBefore(/[\w$]*/);
      if (!word && !context.explicit) {
        return null;
      }
      return {
        from: word ? word.from : pos,
        options: topLevel
      };
    };
  }

  function picoEditorTheme(cm) {
    // Colour tokens from Pico CSS variables so light/dark follow the site theme.
    var highlight = cm.HighlightStyle.define([
      { tag: cm.tags.comment, color: "var(--pico-muted-color)" },
      { tag: cm.tags.keyword, color: "var(--pico-primary)" },
      { tag: cm.tags.controlKeyword, color: "var(--pico-primary)" },
      { tag: cm.tags.string, color: "var(--pico-secondary)" },
      { tag: cm.tags.number, color: "var(--pico-secondary)" },
      { tag: cm.tags.bool, color: "var(--pico-secondary)" },
      { tag: cm.tags.null, color: "var(--pico-secondary)" },
      { tag: cm.tags.function(cm.tags.variableName), color: "var(--pico-primary)" },
      { tag: cm.tags.definition(cm.tags.variableName), color: "var(--pico-color)" },
      { tag: cm.tags.propertyName, color: "var(--pico-color)" },
      { tag: cm.tags.typeName, color: "var(--pico-secondary)" },
      { tag: cm.tags.className, color: "var(--pico-secondary)" },
      { tag: cm.tags.operator, color: "var(--pico-color)" },
      { tag: cm.tags.punctuation, color: "var(--pico-muted-color)" }
    ]);
    var theme = cm.EditorView.theme({
      "&": {
        height: "100%",
        color: "var(--pico-color)",
        backgroundColor: "var(--pico-background-color)"
      },
      ".cm-content": {
        fontFamily: "ui-monospace, \"Cascadia Code\", Consolas, monospace",
        fontSize: "0.85rem",
        caretColor: "var(--pico-color)",
        minHeight: "10rem"
      },
      ".cm-scroller": {
        fontFamily: "ui-monospace, \"Cascadia Code\", Consolas, monospace",
        lineHeight: "1.45"
      },
      ".cm-gutters": {
        backgroundColor: "var(--pico-background-color)",
        color: "var(--pico-muted-color)",
        borderRight: "1px solid var(--pico-muted-border-color)"
      },
      ".cm-activeLineGutter": {
        backgroundColor: "color-mix(in srgb, var(--pico-muted-color) 8%, transparent)",
        color: "var(--pico-color)"
      },
      ".cm-activeLine": {
        backgroundColor: "color-mix(in srgb, var(--pico-muted-color) 8%, transparent)"
      },
      "&.cm-focused": {
        outline: "2px solid var(--pico-primary-focus)"
      },
      ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
        backgroundColor: "color-mix(in srgb, var(--pico-primary) 25%, transparent)"
      },
      ".cm-cursor": {
        borderLeftColor: "var(--pico-color)"
      },
      ".cm-tooltip": {
        backgroundColor: "var(--pico-background-color)",
        color: "var(--pico-color)",
        border: "1px solid var(--pico-muted-border-color)"
      },
      ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
        backgroundColor: "color-mix(in srgb, var(--pico-primary) 20%, transparent)",
        color: "var(--pico-color)"
      },
      ".cm-completionIcon": {
        color: "var(--pico-muted-color)"
      },
      ".cm-completionDetail": {
        color: "var(--pico-muted-color)",
        fontStyle: "normal",
        marginLeft: "0.5rem"
      },
      ".cm-panels": {
        backgroundColor: "var(--pico-background-color)",
        color: "var(--pico-color)"
      },
      ".cm-panel": {
        color: "var(--pico-color)"
      },
      ".cm-panel.cm-search": {
        backgroundColor: "var(--pico-background-color)",
        color: "var(--pico-color)"
      },
      ".cm-panel.cm-search input, .cm-panel.cm-search button, .cm-panel.cm-search label": {
        color: "var(--pico-color)"
      },
      ".cm-searchMatch": {
        backgroundColor: "color-mix(in srgb, var(--pico-secondary) 35%, transparent)"
      },
      ".cm-searchMatch-selected": {
        backgroundColor: "color-mix(in srgb, var(--pico-primary) 40%, transparent)"
      },
      ".cm-matchingBracket": {
        backgroundColor: "color-mix(in srgb, var(--pico-primary) 25%, transparent)",
        outline: "1px solid var(--pico-primary)"
      },
      ".cm-nonmatchingBracket": {
        color: "var(--pico-primary-hover)",
        outline: "1px solid var(--pico-primary-hover)"
      }
    });
    return [theme, cm.syntaxHighlighting(highlight)];
  }

  function scriptEditor(node, collapsed) {
    var onInput = function (value) {
      node.prompt = value;
      sync();
    };
    var fallback = textarea(node.prompt, 10, onInput);
    fallback.className = "wf-script";
    var wrap = el("div", { className: "wf-cm" });
    wrap.appendChild(fallback);

    function degrade(message) {
      console.warn("MidWare: " + message + ", keeping textarea.");
      if (!wrap.querySelector(".wf-cm-note")) {
        wrap.appendChild(el("small", {
          className: "wf-cm-note",
          text: "Plain editor (CodeMirror unavailable)."
        }));
      }
    }

    // A collapsed card pays nothing: skip the import/mount entirely. Expanding
    // re-renders the card and mounts the editor then.
    if (collapsed) {
      return wrap;
    }

    loadCodeMirror().then(function (cm) {
      // The card may have been redrawn (collapsed/moved/deleted) while the
      // import was in flight — bail instead of mounting into a dead subtree.
      // parentNode alone is not enough: the detached wrap is still fallback's
      // parent, so only a connectivity check catches the redraw case.
      if (!fallback.isConnected) {
        return;
      }
      try {
        var completionSource = makeJsCompletionSource(cm);
        var jsSupport = cm.javascript();
        var view = new cm.EditorView({
          parent: wrap,
          state: cm.EditorState.create({
            doc: fallback.value,
            extensions: [
              cm.basicSetup,
              jsSupport,
              // basicSetup already wires autocompletion(); feed our source
              // through the language-data facet so it merges instead of
              // spawning a second plugin via a second autocompletion() call.
              jsSupport.language.data.of({ autocomplete: completionSource }),
              picoEditorTheme(cm),
              cm.EditorView.lineWrapping,
              cm.EditorView.updateListener.of(function (update) {
                if (update.docChanged) {
                  onInput(update.state.doc.toString());
                }
              })
            ]
          })
        });
        fallback.remove();
        wrap._cmView = view;
      } catch (error) {
        degrade("CodeMirror mount failed");
      }
    }).catch(function () {
      degrade("CodeMirror unavailable");
    });

    return wrap;
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
      // selects, buttons and the CodeMirror surface keep their normal mouse
      // behaviour (typing / selection / gutter drag).
      var blocked = event.target && event.target.closest &&
        event.target.closest(
          "input, textarea, select, button, label, " +
          ".cm-content, .cm-gutters, .cm-editor, .cm-scroller, .cm-tooltip, .cm-panels"
        );
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

  function nodePeek(node) {
    var head = node.name || "node";
    if (node.type === "environment") {
      var actions = (node.actions || []).map(function (value) {
        return ENV_ACTION_LABELS_MAP[value] || value;
      });
      return head + " · " + (actions.join(", ") || "no actions");
    }
    if (node.type === "assure") {
      var text = head + " · " + (node.left || "") + " " + (node.operator || "equals");
      if (!ASSURE_UNARY[node.operator]) {
        text += " " + (node.right || "");
      }
      return text.replace(/\s+/g, " ").slice(0, 110);
    }
    if (node.type === "context") {
      var trim = node.trim_count
        ? "keep last " + node.trim_count
        : "all messages";
      if (node.trim_drop_system) {
        trim += ", no system";
      }
      if (node.trim_drop_images) {
        trim += ", no images";
      }
      return head + " · " + trim;
    }
    if (node.type === "javascript") {
      var lines = String(node.prompt || "").split("\n").length;
      return head + " · JS · " + lines + " line" + (lines === 1 ? "" : "s") +
        (node.allow_network ? " · network" : "");
    }
    return head + " · " + (node.model || "premodel default") + " — " +
      String(node.prompt || "").replace(/\s+/g, " ").slice(0, 90);
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
        if (node.type === "environment" && !Array.isArray(node.actions)) {
          node.actions = ["time"];
        }
        if (node.type === "javascript") {
          if (!node.timeout_seconds || node.timeout_seconds > 30) {
            node.timeout_seconds = 5;
          }
          if (!node.prompt) {
            node.prompt = JS_SCRIPT_STARTER;
          }
        }
        if (node.type === "assure") {
          if (typeof node.left !== "string") {
            node.left = "";
          }
          if (typeof node.right !== "string") {
            node.right = "";
          }
          if (!node.operator) {
            node.operator = "equals";
          }
        }
        sync();
        render();
      }
    });
    Object.keys(NODE_TYPE_LABELS).forEach(function (type) {
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
    } else if (node.type === "environment") {
      var actionsWrap = el("div", { className: "wf-checklist" });
      ENV_ACTION_OPTIONS.forEach(function (pair) {
        var value = pair[0];
        actionsWrap.appendChild(
          el("label", { className: "wf-inline" }, [
            checkboxInput((node.actions || []).indexOf(value) !== -1, function (on) {
              var list = (node.actions || []).slice();
              var at = list.indexOf(value);
              if (on && at === -1) {
                list.push(value);
              }
              if (!on && at !== -1) {
                list.splice(at, 1);
              }
              node.actions = list;
              sync();
            }),
            document.createTextNode(" " + pair[1])
          ])
        );
      });
      typeFields = [
        labelField("Actions", actionsWrap, "Runs locally — no sub-request, no tokens."),
        labelField(
          "Timezone",
          el("label", { className: "wf-inline" }, [
            checkboxInput(node.use_system_timezone !== false, function (on) {
              node.use_system_timezone = on;
              sync();
            }),
            document.createTextNode(" Use system timezone when formatting is called")
          ]),
          "Off = UTC."
        ),
        el("div", { className: "wf-vars" }, [
          el("code", {
            className: "wf-var",
            text: "{{ " + (node.name || "node") + ".time.unix }}"
          }),
          el("code", {
            className: "wf-var",
            text: "{{ " + (node.name || "node") + ".time.formatted(\"HH:MN\") }}"
          })
        ]),
        el("p", {
          className: "wf-card-note",
          text: "Format tokens: HH hour, MN minute (not month — MM is the month!), SE second, DD day, YY / YYYY year."
        })
      ];
    } else if (node.type === "assure") {
      var leftInput = textInput(node.left, function (value) {
        node.left = value;
        sync();
      }, { type: "text", placeholder: "left side" });
      var opPicker = el("select", {
        onchange: function (event) {
          node.operator = event.target.value;
          sync();
          render();
        }
      });
      ASSURE_OPERATOR_OPTIONS.forEach(function (pair) {
        var option = el("option", { value: pair[0], text: pair[1] });
        if ((node.operator || "equals") === pair[0]) {
          option.setAttribute("selected", "selected");
        }
        opPicker.appendChild(option);
      });
      var rightInput = textInput(node.right, function (value) {
        node.right = value;
        sync();
      }, { type: "text", placeholder: "right side" });
      typeFields = [
        labelField("Left", leftInput, "Supports {{ variables }} — rendered before comparison."),
        labelField("Operator", opPicker),
        ASSURE_UNARY[node.operator] ? null :
          labelField("Right", rightInput, "Supports {{ variables }}."),
        el("p", {
          className: "wf-card-note",
          text: "If the check is false the workflow stops with an error before the Final Request."
        })
      ];
    } else if (node.type === "context") {
      var trimInput = textInput(node.trim_count || 0, function (value) {
        var parsed = parseInt(value, 10);
        node.trim_count = isNaN(parsed) || parsed < 1 ? 0 : Math.min(parsed, 1000);
        sync();
      }, { type: "number", min: "0", step: "1", placeholder: "0 = all" });
      typeFields = [
        labelField(
          "Trim",
          trimInput,
          "Keep only the last N messages in trimmed (0 = keep all)."
        ),
        el("div", { className: "wf-grid" }, [
          el("label", { className: "wf-inline" }, [
            checkboxInput(node.trim_drop_system, function (value) {
              node.trim_drop_system = value;
              sync();
            }),
            document.createTextNode(" Drop system messages")
          ]),
          el("label", { className: "wf-inline" }, [
            checkboxInput(node.trim_drop_images, function (value) {
              node.trim_drop_images = value;
              sync();
            }),
            document.createTextNode(" Drop images")
          ])
        ]),
        el("div", { className: "wf-vars" }, [
          ["count", "user_count", "assistant_count", "image_count", "chars"].map(function (key) {
            return el("code", { className: "wf-var", text: "{{ " + (node.name || "node") + "." + key + " }}" });
          }),
          ["text", "last_user", "last_assistant", "first_user", "trimmed"].map(function (key) {
            return el("code", { className: "wf-var", text: "{{ " + (node.name || "node") + "." + key + " }}" });
          })
        ].reduce(function (all, part) { return all.concat(part); }, [])),
        el("p", {
          className: "wf-card-note",
          text: "Reads the caller's conversation at this point in the chain. trimmed is a JSON string shaped by the options above; text is the conversation flattened to plain text."
        })
      ];
    } else if (node.type === "javascript") {
      var scriptBox = scriptEditor(node, node.collapsed);
      var jsTimeout = textInput(node.timeout_seconds == null ? 5 : node.timeout_seconds, function (value) {
        var parsed = parseInt(value, 10);
        node.timeout_seconds = isNaN(parsed) || parsed < 1 ? 5 : Math.min(parsed, 30);
        sync();
      }, { type: "number", min: "1", max: "30" });
      typeFields = [
        labelField(
          "Script",
          scriptBox,
          "Define async function run(midware). Uses the bundled mini-racer engine."
        ),
        el("div", { className: "wf-grid" }, [
          el("label", { className: "wf-inline" }, [
            checkboxInput(node.allow_network, function (value) {
              node.allow_network = value;
              sync();
            }),
            document.createTextNode(" Allow network (midware.fetch)")
          ]),
          labelField("Timeout (s)", jsTimeout)
        ]),
        el("p", {
          className: "wf-card-note",
          text: "midware.messages (the caller's conversation), midware.vars (earlier variables), midware.systemRequest. Use midware.set(k, v) to publish {{ " + (node.name || "node") + ".<k> }}, midware.setMessages(arr) to rewrite the context, await midware.fetch(url, opts) for HTTP, and await midware.chat({messages, model}) (or the APIRequest class) to ask a model through the connection above. The APIImageRequest class generates images the same way — create({prompt, ...}) or ask(prompt) returns their url. The return value and console.log go to the node's reasoning."
        }),
        el("p", {
          className: "wf-card-note",
          text: "Sandboxed V8: 16 MB memory cap, no filesystem. midware.fetch needs the network box; midware.chat goes through the connection (key stays server-side) and does not. The editor autocompletes midware.* and the sandbox's real globals; it falls back to a plain textarea if CodeMirror cannot load."
        })
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

    var nameHint = "Variable prefix: {{ " + (node.name || "node") + ".content }}";
    if (node.type === "environment") {
      nameHint = "Variables: {{ " + (node.name || "node") + ".time.unix }}";
    } else if (node.type === "context") {
      nameHint = "Variables: {{ " + (node.name || "node") + ".count }}, .text, .trimmed …";
    } else if (node.type === "javascript") {
      nameHint = "Anything you midware.set() appears as {{ " + (node.name || "node") + ".<key> }}";
    } else if (node.type === "assure") {
      nameHint = null;
    }
    var bodyChildren = [labelField("Name", nameInput, nameHint)];
    if (REQUEST_TYPES[node.type] || node.type === "javascript") {
      bodyChildren.push(
        el("div", { className: "wf-grid" }, [
          labelField("Connection", routePicker),
          labelField("Model", modelInput)
        ])
      );
    }
    var body = el("div", { className: "wf-card-body" }, bodyChildren.concat(typeFields));

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
        el("p", { className: "wf-node-peek", text: nodePeek(node) })
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
      ),
      nodeOption(
        "environment",
        "Environment",
        "Declare variables from the environment — e.g. the current time — without any sub-request."
      ),
      nodeOption(
        "context",
        "Context",
        "Inspect the caller's conversation: counts, flattened text, last user message, a trimmed copy."
      ),
      nodeOption(
        "assure",
        "Assure",
        "Guard clause: compare two templated strings; a failed check stops the workflow early."
      ),
      nodeOption(
        "javascript",
        "JavaScript",
        "Sandboxed V8 script for when cards aren't enough: loop, branch, rewrite the context, fetch."
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

  // --- UNSTABLE BETA gate ---------------------------------------------------
  // First time someone flips the switch, ask once with the rough-waters
  // modal. "I'm feeling lucky" acknowledges forever (localStorage);
  // "no, go back" (or the backdrop) just closes — the switch stays off and
  // the modal will ask again next time they try.
  var BETA_ACK_KEY = "mw_premodel_workflows_beta";
  var betaModal = null;

  function betaAcknowledged() {
    try {
      return window.localStorage.getItem(BETA_ACK_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function closeBetaModal() {
    if (betaModal) {
      betaModal.hidden = true;
    }
  }

  function acceptBeta() {
    try {
      window.localStorage.setItem(BETA_ACK_KEY, "1");
    } catch (e) {
      // private mode etc. — worst case it asks again next time
    }
    closeBetaModal();
    checkbox.checked = true;
    updatePanels();
  }

  function openBetaModal() {
    if (!betaModal) {
      betaModal = el("div", { className: "wf-modal", hidden: "hidden" });
      betaModal.addEventListener("click", function (event) {
        if (event.target === betaModal) {
          closeBetaModal();
        }
      });
      betaModal.appendChild(el("div", { className: "wf-modal-panel" }, [
        el("div", { className: "wf-modal-header" }, [
          el("strong", { text: "Premodel Workflows are in pretty much rough waters." })
        ]),
        el("p", { text: "And this crazy man wants you to TEST them!" }),
        el("p", {
          text: "Premodel workflows are a different approach to Premodels. Instead of " +
                "being able to modify only the system prompt, workflows allows you to " +
                "modify the CONTEXT!"
        }),
        el("p", {
          text: "Workflows are not complete. They never will be. They are also heavily " +
                "underdocumented as of now."
        }),
        el("p", {
          text: "Most control will be provided via the JavaScript Node. Though we " +
                "expose some controls on the block side of workflows."
        }),
        el("p", {
          text: "if you need to alter context or do crazy workflows, use javascript " +
                "nodes. If you want something simple, use Workflow Nodes."
        }),
        el("p", {
          text: "Also... by clicking \"I'm feeling lucky\" you're acknowledging that " +
                "premodel workflows are insanely unstable and could cause some silly " +
                "problems! Or even an RCE!"
        }),
        el("p", { text: "Good luck, friend." }),
        el("div", { className: "wf-actions" }, [
          el("button", {
            type: "button",
            className: "secondary outline",
            text: "no, go back",
            onclick: function () {
              closeBetaModal();
            }
          }),
          el("button", {
            type: "button",
            text: "I'm feeling lucky",
            onclick: acceptBeta
          })
        ])
      ]));
      document.body.appendChild(betaModal);
    }
    betaModal.hidden = false;
  }

  checkbox.addEventListener("change", function () {
    if (checkbox.checked && !betaAcknowledged()) {
      checkbox.checked = false;
      updatePanels();
      openBetaModal();
      return;
    }
    updatePanels();
  });
  loadGraph();
  sync();
  render();
  updatePanels();
})();
