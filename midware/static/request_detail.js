(function () {
  "use strict";

  const section = document.getElementById("conversation");
  const list = document.getElementById("message-list");
  const status = document.getElementById("conversation-status");
  if (!section || !list) {
    return;
  }

  const endpoint = section.dataset.source;

  function el(tag, className, text) {
    const node = document.createElement(tag);
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

  function blockTitle(block) {
    return "Message " + block.number + " · " + block.role;
  }

  function roleClass(block) {
    const role = String(block.role || "").toLowerCase();
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
    const bits = [];
    bits.push(block.source === "response" ? "response" : "request");
    if (block.name) {
      bits.push("name: " + block.name);
    }
    if (block.tool_call_id) {
      bits.push("tool_call_id: " + block.tool_call_id);
    }
    const reasoning = count(block.reasoning);
    if (reasoning) {
      bits.push(reasoning + " reasoning");
    }
    const tools = count(block.tool_calls);
    if (tools) {
      bits.push(tools + " tool call" + (tools === 1 ? "" : "s"));
    }
    if (block.extra && block.extra.finish_reason) {
      bits.push("finish: " + block.extra.finish_reason);
    }
    return bits.join(" · ");
  }

  function textPre(text, className) {
    const pre = el("pre", "msg-text mono");
    if (className) {
      pre.classList.add(className);
    }
    pre.textContent = text && text.length ? text : "(empty)";
    return pre;
  }

  function reasoningBlock(text, index) {
    const details = el("details", "reasoning");
    const summary = el("summary", null, "Thinking" + (index ? " " + index : ""));
    details.appendChild(summary);
    details.appendChild(textPre(text, "reasoning-text"));
    return details;
  }

  function renderBlock(block) {
    const article = el("details", "message " + roleClass(block));
    article.dataset.number = block.number;
    article.dataset.role = block.role;
    article.dataset.source = block.source;

    const summary = el("summary", "message-head");
    const badge = el("span", "message-badge", String(block.number));
    const title = el("span", "message-title", blockTitle(block));
    const meta = el("span", "message-meta", metaLine(block));
    summary.appendChild(badge);
    summary.appendChild(title);
    summary.appendChild(meta);
    article.appendChild(summary);

    const body = el("div", "message-body");

    (block.reasoning || []).forEach(function (text, i) {
      body.appendChild(reasoningBlock(text, i + 1));
    });

    body.appendChild(textPre(block.text));

    if (count(block.tool_calls)) {
      const tools = el("details", "tool-calls");
      tools.appendChild(el("summary", null, "Tool calls"));
      tools.appendChild(textPre(block.tool_calls.join("\n\n")));
      body.appendChild(tools);
    }

    const extraKeys = block.extra ? Object.keys(block.extra).filter(function (key) {
      return key !== "finish_reason";
    }) : [];
    if (extraKeys.length) {
      const extra = el("details", "extra");
      extra.appendChild(el("summary", null, "Other fields"));
      const clone = {};
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
    const wanted = new Set();
    let lastUser = -1;
    let lastAssistant = -1;
    blocks.forEach(function (block, i) {
      const role = String(block.role || "").toLowerCase();
      if (role === "user") {
        lastUser = i;
      } else if (role === "assistant" || role === "model") {
        lastAssistant = i;
      }
    });
    if (lastUser >= 0) {
      wanted.add(lastUser);
    }
    if (lastAssistant >= 0) {
      wanted.add(lastAssistant);
    }
    return wanted;
  }

  function render(conversation) {
    const blocks = conversation.messages || [];
    list.textContent = "";
    const open = openByDefault(blocks);
    blocks.forEach(function (block, i) {
      const node = renderBlock(block);
      if (open.has(i)) {
        node.open = true;
      }
      list.appendChild(node);
    });
    if (status) {
      status.textContent = blocks.length + " message" + (blocks.length === 1 ? "" : "s") +
        (blocks.length ? " · last user + assistant opened" : "");
    }
  }

  function readInline() {
    const node = document.getElementById("conversation-data");
    if (!node) {
      return null;
    }
    try {
      return JSON.parse(node.textContent);
    } catch (err) {
      return null;
    }
  }

  const inline = readInline();
  if (inline) {
    render(inline);
  }

  const fetchButton = document.getElementById("fetch-messages");
  if (fetchButton && endpoint) {
    fetchButton.addEventListener("click", function () {
      fetchButton.disabled = true;
      fetch(endpoint, { headers: { Accept: "application/json" } })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("HTTP " + response.status);
          }
          return response.json();
        })
        .then(function (conversation) {
          render(conversation);
          const blob = new Blob([JSON.stringify(conversation, null, 2)], { type: "application/json" });
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          link.download = "midware-request-messages.json";
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          URL.revokeObjectURL(url);
        })
        .catch(function (error) {
          if (status) {
            status.textContent = "Fetch failed: " + error.message;
          }
        })
        .finally(function () {
          fetchButton.disabled = false;
        });
    });
  }

  const expandAll = document.getElementById("expand-all");
  if (expandAll) {
    expandAll.addEventListener("click", function () {
      list.querySelectorAll("details").forEach(function (node) {
        node.open = true;
      });
    });
  }

  const collapseAll = document.getElementById("collapse-all");
  if (collapseAll) {
    collapseAll.addEventListener("click", function () {
      list.querySelectorAll("details").forEach(function (node) {
        node.open = false;
      });
    });
  }
})();
