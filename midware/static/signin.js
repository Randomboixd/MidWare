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
    var who = data.label || "the provider";
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

  var widgets = document.querySelectorAll("[data-signin]");
  Array.prototype.forEach.call(widgets, function (widget) {
    var buttons = widget.querySelectorAll("[data-signin-provider]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var provider = button.getAttribute("data-signin-provider");
        if (provider === "openrouter") {
          startOpenRouter(widget);
        } else {
          startNanoGPT(widget, provider);
        }
      });
    });
  });
})();
