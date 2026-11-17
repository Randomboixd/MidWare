/* Request inspector: payload colouring, copy buttons, the route's live
 * allowance, and the conversation.
 *
 * Payloads are coloured the first time their fold is opened: a stored stream
 * can run to hundreds of kilobytes and colouring one nobody looked at is work
 * spent for nothing. Everything here reads DOM the template already rendered,
 * so the page is complete without JavaScript — only the colour and the message
 * bodies need it. */
(function () {
  "use strict";

  /* -- helpers ----------------------------------------------------------- */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function count(value) {
    return Array.isArray(value) ? value.length : 0;
  }

  function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* -- payload colouring -------------------------------------------------- */

  // A string, a number or a literal, wherever they appear. A key is the same
  // string token followed by a colon, which is what tells the two apart.
  var JSON_TOKEN = /("(?:\\.|[^"\\\n])*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b/g;

  function jsonHtml(text) {
    var out = [];
    var last = 0;
    var match;
    JSON_TOKEN.lastIndex = 0;
    while ((match = JSON_TOKEN.exec(text)) !== null) {
      if (match.index > last) {
        out.push(escapeHtml(text.slice(last, match.index)));
      }
      if (match[1] !== undefined) {
        if (match[2] !== undefined) {
          out.push('<span class="tok-k">', escapeHtml(match[1]), "</span>");
          out.push('<span class="tok-p">', escapeHtml(match[2]), "</span>");
        } else {
          out.push('<span class="tok-s">', escapeHtml(match[1]), "</span>");
        }
      } else if (match[3] !== undefined) {
        out.push('<span class="tok-n">', match[3], "</span>");
      } else {
        out.push('<span class="tok-b">', match[4], "</span>");
      }
      last = JSON_TOKEN.lastIndex;
    }
    if (last < text.length) {
      out.push(escapeHtml(text.slice(last)));
    }
    return out.join("");
  }

  function sseHtml(text) {
    var out = [];
    var lines = text.split("\n");
    for (var i = 0; i < lines.length; i += 1) {
      var line = lines[i];
      if (line.indexOf("data:") === 0) {
        var body = line.slice(5);
        var spaced = body.charAt(0) === " ";
        out.push('<span class="tok-p">data:</span>');
        if (spaced) {
          out.push(" ");
          body = body.slice(1);
        }
        out.push(body === "[DONE]" ? '<span class="tok-e">[DONE]</span>' : jsonHtml(body));
      } else if (line.indexOf("event:") === 0 || line.indexOf("id:") === 0) {
        out.push('<span class="tok-e">', escapeHtml(line), "</span>");
      } else {
        out.push(escapeHtml(line));
      }
      if (i < lines.length - 1) {
        out.push("\n");
      }
    }
    return out.join("");
  }

  function paint(pre) {
    if (!pre || pre.dataset.painted === "1") {
      return;
    }
    var code = pre.querySelector("code");
    if (!code) {
      return;
    }
    var text = code.textContent || "";
    var kind = pre.getAttribute("data-lang") || "";
    if (kind === "sse") {
      code.innerHTML = sseHtml(text);
    } else if (kind === "json" || kind === "json-partial") {
      code.innerHTML = jsonHtml(text);
    } else {
      return; // a plain or HTML body reads better uncoloured
    }
    pre.dataset.painted = "1";
  }

  function paintAll(scope) {
    Array.prototype.forEach.call(scope.querySelectorAll("pre.payload-code"), paint);
  }

  Array.prototype.forEach.call(document.querySelectorAll("pre.payload-code"), function (pre) {
    var fold = pre.closest("details");
    if (!fold || fold.open) {
      paint(pre);
    }
  });

  document.addEventListener(
    "toggle",
    function (event) {
      var fold = event.target;
      if (fold && fold.open && fold.querySelector) {
        paintAll(fold);
      }
    },
    true
  );

  /* -- copy buttons ------------------------------------------------------- */

  Array.prototype.forEach.call(document.querySelectorAll("[data-copy]"), function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.getAttribute("data-copy"));
      var text = target ? target.textContent || "" : "";
      window.mwCopy(text).then(function (ok) {
        window.mwFlash(button, ok ? "Copied" : "Copy failed");
      });
    });
  });

  /* -- the route's live allowance ----------------------------------------- */

  Array.prototype.forEach.call(document.querySelectorAll(".mw-allowance[data-balance-url]"), function (cell) {
    fetch(cell.getAttribute("data-balance-url"), { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.json().catch(function () {
          return {};
        });
      })
      .then(function (body) {
        cell.textContent = body && body.ok && body.text ? body.text : "not available";
      })
      .catch(function () {
        cell.textContent = "not available";
      });
  });

  /* -- the conversation --------------------------------------------------- */

  var section = document.getElementById("conversation");
  var list = document.getElementById("message-list");
  var status = document.getElementById("conversation-status");
  if (!section || !list) {
    return;
  }

  function blockTitle(block) {
    return "Message " + block.number + ", " + block.role;
  }

  function roleClass(block) {
    var role = String(block.role || "").toLowerCase();
    if (role === "user") {
      return "is-user";
    }
    if (role === "assistant" || role === "model") {
      return "is-assistant";
    }
    if (role === "system" || role === "developer") {
      return "is-system";
    }
    if (role === "tool" || role === "function") {
      return "is-tool";
    }
    return "is-other";
  }

  function metaLine(block) {
    var bits = [];
    bits.push(block.source === "response" ? "response" : "request");
    if (block.name) {
      bits.push("name: " + block.name);
    }
    if (block.tool_call_id) {
      bits.push("tool_call_id: " + block.tool_call_id);
    }
    var reasoning = count(block.reasoning);
    if (reasoning) {
      bits.push(reasoning + " reasoning");
    }
    var tools = count(block.tool_calls);
    if (tools) {
      bits.push(tools + " tool call" + (tools === 1 ? "" : "s"));
    }
    if (block.extra && block.extra.finish_reason) {
      bits.push("finish: " + block.extra.finish_reason);
    }
    var characters = (block.text || "").length;
    if (characters) {
      bits.push(characters.toLocaleString() + " chars");
    }
    return bits.join(", ");
  }

  function textPre(text, className) {
    var pre = el("pre", "msg-text mono");
    if (className) {
      pre.classList.add(className);
    }
    pre.textContent = text && text.length ? text : "(empty)";
    return pre;
  }

  function reasoningBlock(text, index) {
    var details = el("details", "reasoning");
    details.appendChild(el("summary", null, "Thinking" + (index ? " " + index : "")));
    details.appendChild(textPre(text, "reasoning-text"));
    return details;
  }

  function renderBlock(block) {
    var article = el("details", "message " + roleClass(block));
    article.dataset.number = block.number;
    article.dataset.role = block.role;
    article.dataset.source = block.source;

    var summary = el("summary", "message-head");
    summary.appendChild(el("span", "message-badge", String(block.number)));
    summary.appendChild(el("span", "message-title", blockTitle(block)));
    summary.appendChild(el("span", "message-meta", metaLine(block)));
    article.appendChild(summary);

    var body = el("div", "message-body");

    (block.reasoning || []).forEach(function (text, i) {
      body.appendChild(reasoningBlock(text, i + 1));
    });

    body.appendChild(textPre(block.text));

    if (count(block.tool_calls)) {
      var tools = el("details", "tool-calls");
      tools.appendChild(el("summary", null, "Tool calls"));
      tools.appendChild(textPre(block.tool_calls.join("\n\n")));
      body.appendChild(tools);
    }

    var extraKeys = block.extra
      ? Object.keys(block.extra).filter(function (key) {
          return key !== "finish_reason";
        })
      : [];
    if (extraKeys.length) {
      var extra = el("details", "extra");
      extra.appendChild(el("summary", null, "Other fields"));
      var clone = {};
      extraKeys.forEach(function (key) {
        clone[key] = block.extra[key];
      });
      extra.appendChild(textPre(JSON.stringify(clone, null, 2)));
      body.appendChild(extra);
    }

    article.appendChild(body);
    return article;
  }

  function openByDefault(blocks) {
    var wanted = [];
    var lastUser = -1;
    var lastAssistant = -1;
    blocks.forEach(function (block, i) {
      var role = String(block.role || "").toLowerCase();
      if (role === "user") {
        lastUser = i;
      } else if (role === "assistant" || role === "model") {
        lastAssistant = i;
      }
    });
    if (lastUser >= 0) {
      wanted.push(lastUser);
    }
    if (lastAssistant >= 0) {
      wanted.push(lastAssistant);
    }
    return wanted;
  }

  function render(conversation) {
    var blocks = conversation.messages || [];
    list.textContent = "";
    var open = openByDefault(blocks);
    blocks.forEach(function (block, i) {
      var node = renderBlock(block);
      if (open.indexOf(i) >= 0) {
        node.open = true;
      }
      list.appendChild(node);
    });
    if (status) {
      status.textContent =
        blocks.length +
        " message" +
        (blocks.length === 1 ? "" : "s") +
        (blocks.length ? ", last user and assistant open" : "");
    }
  }

  function readInline() {
    var node = document.getElementById("conversation-data");
    if (!node) {
      return null;
    }
    try {
      return JSON.parse(node.textContent);
    } catch (err) {
      return null;
    }
  }

  var inline = readInline();
  if (inline) {
    render(inline);
  }

  var expandAll = document.getElementById("expand-all");
  if (expandAll) {
    expandAll.addEventListener("click", function () {
      Array.prototype.forEach.call(list.querySelectorAll("details"), function (node) {
        node.open = true;
      });
    });
  }

  var collapseAll = document.getElementById("collapse-all");
  if (collapseAll) {
    collapseAll.addEventListener("click", function () {
      Array.prototype.forEach.call(list.querySelectorAll("details"), function (node) {
        node.open = false;
      });
    });
  }
})();
