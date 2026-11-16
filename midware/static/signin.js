(function () {
  "use strict";

  var config = window.MidWareSignin;
  if (!config || !config.start) return;

  // The flow currently waiting on OpenRouter: which form to fill and which
  // state token identifies its handoff, so a stale popup cannot fill the form.
  var activeFlow = null;
  var HANDOFF_PREFIX = "midware-signin:";

  function pruneStaleHandoffs() {
    try {
      var cutoff = Date.now() - 15 * 60 * 1000;
      for (var i = localStorage.length - 1; i >= 0; i--) {
        var storedKey = localStorage.key(i);
        if (!storedKey || storedKey.indexOf(HANDOFF_PREFIX) !== 0) continue;
        try {
          var entry = JSON.parse(localStorage.getItem(storedKey));
          if (!entry || !entry.ts || entry.ts < cutoff) localStorage.removeItem(storedKey);
        } catch (error) {
          localStorage.removeItem(storedKey);
        }
      }
    } catch (error) {}
  }

  pruneStaleHandoffs();

  function post(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload || {})
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok || body.ok === false) {
          throw new Error(body.error || "HTTP " + response.status);
        }
        return body;
      });
    });
  }

  function ui(widget) {
    return {
      status: widget.querySelector("[data-signin-status]"),
      text: widget.querySelector("[data-signin-text]"),
      link: widget.querySelector("[data-signin-link]")
    };
  }

  // OpenCode Go and Zen share one widget but not one panel: Go opens a warning
  // first, Zen goes straight to its key, and each key panel carries its own
  // copy. Everything is looked up by provider so a widget can hold both.
  function panels(widget, provider) {
    var key = widget.querySelector('[data-signin-key="' + (provider || "") + '"]');
    return {
      menu: widget.querySelector("[data-signin-menu]"),
      warning: widget.querySelector("[data-signin-warning]"),
      accept: widget.querySelector("[data-signin-accept]"),
      back: widget.querySelector("[data-signin-back]"),
      cont: widget.querySelector("[data-signin-continue]"),
      keys: widget.querySelectorAll("[data-signin-key]"),
      key: key,
      input: key ? key.querySelector("[data-signin-key-input]") : null,
      submit: key ? key.querySelector("[data-signin-key-submit]") : null,
      keyBack: key ? key.querySelector("[data-signin-key-cancel]") : null
    };
  }

  function show(widget, name, provider) {
    var parts = panels(widget, provider);
    if (!parts.menu) return;
    parts.menu.hidden = name !== "menu";
    parts.warning.hidden = name !== "warning";
    Array.prototype.forEach.call(parts.keys, function (panel) {
      panel.hidden = !(name === "key" && panel === parts.key);
    });
    var status = widget.querySelector("[data-signin-status]");
    if (status) status.hidden = true;
  }

  function setBusy(widget, busy) {
    var buttons = widget.querySelectorAll("[data-signin-provider]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.disabled = busy;
    });
  }

  function setStatus(widget, message, href) {
    var parts = ui(widget);
    parts.status.hidden = false;
    parts.status.classList.remove("signin-error");
    parts.text.textContent = message;
    // The href arrives from the provider; only a real https URL becomes a link.
    if (href && /^https:\/\//.test(href)) {
      parts.link.hidden = false;
      parts.link.href = href;
      parts.link.textContent = "Open the approval page";
    } else {
      parts.link.hidden = true;
      parts.link.removeAttribute("href");
    }
  }

  function setError(widget, message) {
    var parts = ui(widget);
    parts.status.hidden = false;
    parts.status.classList.add("signin-error");
    parts.text.textContent = message;
    parts.link.hidden = true;
    parts.link.removeAttribute("href");
  }

  function formOf(widget) {
    return widget.closest("form");
  }

  function formRouteId(form) {
    var input = form ? form.querySelector('input[name="route_id"]') : null;
    if (!input) return null;
    var value = parseInt(input.value, 10);
    return isNaN(value) ? null : value;
  }

  function fillForm(widget, data) {
    var form = formOf(widget);
    if (!form) return;
    var key = form.querySelector('input[name="upstream_key"]');
    if (key && data.key) key.value = data.key;
    var name = form.querySelector('input[name="name"]');
    if (name && data.name) name.value = data.name;
    var host = form.querySelector('input[name="target_host"]');
    if (host && data.host) host.value = data.host;
    // The tag is what later proves which service's balance API to call.
    var provider = form.querySelector('input[name="provider"]');
    if (provider && data.provider) provider.value = data.provider;
  }

  // Signing in means the route *becomes* that provider's route, so the form is
  // filled and saved right away instead of waiting for a second click. A form
  // still missing required fields (the setup code on first run) is left filled
  // for the human to finish.
  function saveRoute(widget, data) {
    fillForm(widget, data);
    var form = formOf(widget);
    // ``detail`` carries anything the provider said about the account (Go's
    // remaining quota), so the confirmation reads as more than "signed in".
    var who = (data.label || "the provider") + (data.detail ? " \u2014 " + data.detail : "");
    if (!form) {
      setStatus(widget, "Signed in with " + who + " — the key is in the form. Save the route to finish.");
      return;
    }
    if (!form.checkValidity()) {
      setStatus(widget, "Signed in with " + who + " — complete the remaining fields, then save.");
      setBusy(widget, false);
      try { form.reportValidity(); } catch (error) {}
      return;
    }
    setStatus(widget, "Signed in with " + who + " — saving the route…");
    window.setTimeout(function () { form.submit(); }, 250);
  }

  function pollNanoGPT(widget, token, delay, attempt) {
    window.setTimeout(function () {
      if (!widget.isConnected) return;
      post(config.poll, { token: token }).then(function (data) {
        if (data.status === "approved") {
          saveRoute(widget, data);
        } else if (data.status === "pending") {
          if (attempt > 400) {
            setError(widget, "Timed out waiting for approval. Start again.");
            setBusy(widget, false);
            return;
          }
          pollNanoGPT(widget, token, delay, attempt + 1);
        } else {
          setError(widget, data.error || "Sign-in ended. Start again.");
          setBusy(widget, false);
        }
      }).catch(function (error) {
        setError(widget, error.message);
        setBusy(widget, false);
      });
    }, delay);
  }

  function startNanoGPT(widget, provider) {
    // Same trick as OpenRouter: open the tab synchronously so the browser
    // cannot block it, then aim it at the approval URL once one comes back.
    var popup = window.open("about:blank", "_blank");
    setBusy(widget, true);
    setStatus(widget, "Starting Nano-GPT sign-in…");
    post(config.start, {
      provider: provider,
      route_id: formRouteId(formOf(widget))
    }).then(function (data) {
      if (popup && !popup.closed) {
        popup.location = data.verify_url;
        setStatus(widget, "Approve the sign-in in the opened tab, then wait…", data.verify_url);
      } else {
        setStatus(widget, "Approve the sign-in in your browser, then wait…", data.verify_url);
      }
      pollNanoGPT(widget, data.token, Math.max(1, data.interval || 2) * 1000, 0);
    }).catch(function (error) {
      if (popup && !popup.closed) popup.close();
      setError(widget, error.message);
      setBusy(widget, false);
    });
  }

  function startOpenRouter(widget) {
    // Open the popup synchronously so the browser cannot block it, then aim it
    // at the authorize URL once the server hands one back.
    var popup = window.open("about:blank", "_blank");
    setBusy(widget, true);
    setStatus(widget, "Waiting for OpenRouter authorization…");
    post(config.start, {
      provider: "openrouter",
      route_id: formRouteId(formOf(widget))
    }).then(function (data) {
      activeFlow = { widget: widget, token: data.token };
      setBusy(widget, false);
      if (popup && !popup.closed) {
        popup.location = data.authorize_url;
        setStatus(widget, "Authorize MidWare in the opened tab. The key returns here automatically.");
      } else {
        setStatus(widget, "Allow popups for automatic sign-in, or open the authorization page yourself.", data.authorize_url);
      }
    }).catch(function (error) {
      if (popup && !popup.closed) popup.close();
      setError(widget, error.message);
      setBusy(widget, false);
    });
  }

  function consumeFlow(flowId, data) {
    var flow = activeFlow;
    if (!flow || !flow.widget.isConnected) return false;
    if (flowId && flow.token && flowId !== flow.token) return false;
    activeFlow = null;
    if (data.error) {
      setError(flow.widget, data.error);
      return true;
    }
    saveRoute(flow.widget, data);
    return true;
  }

  window.addEventListener("message", function (event) {
    if (event.origin !== window.location.origin) return;
    var data = event.data;
    if (!data || data.type !== "midware-signin") return;
    if (consumeFlow(data.flow || null, data) && data.flow) {
      // Both channels may fire; clear the storage copy so the key does not linger.
      try { localStorage.removeItem(HANDOFF_PREFIX + data.flow); } catch (error) {}
    }
  });

  window.addEventListener("storage", function (event) {
    if (!event.key || event.key.indexOf(HANDOFF_PREFIX) !== 0) return;
    var data;
    try {
      data = JSON.parse(event.newValue);
    } catch (error) {
      return;
    }
    if (!data || data.type !== "midware-signin") return;
    var flowId = event.key.slice(HANDOFF_PREFIX.length);
    if (consumeFlow(flowId, data)) {
      try { localStorage.removeItem(event.key); } catch (error) {}
    }
  });

  function openWarning(widget, provider) {
    show(widget, "warning", provider);
    var parts = panels(widget, provider);
    if (parts.accept) parts.accept.checked = false;
    if (parts.cont) parts.cont.disabled = true;
  }

  function openKeyPanel(widget, provider) {
    show(widget, "key", provider);
    var parts = panels(widget, provider);
    if (parts.input) {
      parts.input.value = "";
      parts.input.focus();
    }
    if (parts.submit) parts.submit.disabled = false;
  }

  // The key is verified before the form sees it: a key OpenCode rejects, or one
  // from a workspace without the plan the button names, must never be tagged as
  // that gateway's route.
  function connectKey(widget, provider) {
    var parts = panels(widget, provider);
    var key = parts.input ? parts.input.value.trim() : "";
    if (!key) {
      setError(widget, "Paste an OpenCode API key first.");
      return;
    }
    parts.submit.disabled = true;
    setStatus(widget, "Checking the key with OpenCode\u2026");
    post(config.key, {
      provider: provider,
      key: key,
      route_id: formRouteId(formOf(widget))
    })
      .then(function (data) {
        if (parts.input) parts.input.value = "";
        data.detail = data.quota || "";
        saveRoute(widget, data);
      })
      .catch(function (error) {
        parts.submit.disabled = false;
        setError(widget, error.message);
      });
  }

  // One control set per widget; the two key panels are wired by provider, so no
  // widget has to remember which one is open.
  function wireKeyPanel(widget, provider) {
    var parts = panels(widget, provider);
    if (!parts.key) return;
    if (parts.keyBack) {
      parts.keyBack.addEventListener("click", function () { show(widget, "menu"); });
    }
    if (parts.submit) {
      parts.submit.addEventListener("click", function () { connectKey(widget, provider); });
    }
    if (parts.input) {
      parts.input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          connectKey(widget, provider);
        }
      });
    }
  }

  function keyControls(widget) {
    var base = panels(widget);
    if (base.accept && base.cont) {
      base.accept.addEventListener("change", function () {
        base.cont.disabled = !base.accept.checked;
      });
      // Only Go routes through the warning, so that is the panel it opens.
      base.cont.addEventListener("click", function () { openKeyPanel(widget, "opencode-go"); });
      if (base.back) base.back.addEventListener("click", function () { show(widget, "menu"); });
    }
    wireKeyPanel(widget, "opencode-go");
    wireKeyPanel(widget, "opencode-zen");

    // A form submitted while a key panel is open must not carry untrimmed input.
    var form = formOf(widget);
    if (form) {
      form.addEventListener("submit", function () {
        Array.prototype.forEach.call(
          widget.querySelectorAll("[data-signin-key-input]"),
          function (input) { input.value = input.value.trim(); }
        );
      });
    }
  }

  var widgets = document.querySelectorAll("[data-signin]");
  Array.prototype.forEach.call(widgets, function (widget) {
    keyControls(widget);
    var buttons = widget.querySelectorAll("[data-signin-provider]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var provider = button.getAttribute("data-signin-provider");
        if (provider === "openrouter") {
          startOpenRouter(widget);
        } else if (provider === "opencode-go") {
          openWarning(widget, provider);
        } else if (provider === "opencode-zen") {
          // Nothing to warn about: Zen bills credits per request, no plan risks.
          openKeyPanel(widget, provider);
        } else {
          startNanoGPT(widget, provider);
        }
      });
    });
  });
})();
