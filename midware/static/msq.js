(function () {
  "use strict";

  var WARNING_KEY = "midware.msq.warning.dismissed";

  function initWarning() {
    var banner = document.getElementById("msq-warning");
    if (!banner) {
      return;
    }
    var dismissed = false;
    try {
      dismissed = window.localStorage.getItem(WARNING_KEY) === "1";
    } catch (error) {
      dismissed = false;
    }
    banner.hidden = dismissed;

    var button = document.getElementById("msq-warning-dismiss");
    if (button) {
      button.addEventListener("click", function () {
        banner.hidden = true;
        try {
          window.localStorage.setItem(WARNING_KEY, "1");
        } catch (error) {
          // Storage disabled (private mode); the banner simply reappears next visit.
        }
      });
    }
  }

  initWarning();

  var SVG_NS = "http://www.w3.org/2000/svg";
  var WIDTH = 300;
  var HEIGHT = 90;
  var PAD = 12;

  function el(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs || {}).forEach(function (key) {
      node.setAttribute(key, attrs[key]);
    });
    return node;
  }

  function fmtMs(value) {
    if (value === null || value === undefined || value === "") {
      return "—";
    }
    var ms = Number(value);
    if (isNaN(ms)) {
      return "—";
    }
    if (ms >= 1000) {
      return (ms / 1000).toFixed(2) + "s";
    }
    return Math.round(ms) + "ms";
  }

  function fmtTime(ms) {
    if (!ms) {
      return "unknown time";
    }
    try {
      return new Date(ms).toLocaleString();
    } catch (error) {
      return "unknown time";
    }
  }

  function scoreY(score) {
    var inner = HEIGHT - PAD * 2;
    return PAD + (1 - Math.max(0, Math.min(100, score)) / 100) * inner;
  }

  function buildTooltip(point) {
    var wrap = document.createElement("div");
    wrap.className = "msq-tip-inner";

    var head = document.createElement("div");
    head.className = "msq-tip-head";
    head.textContent = fmtTime(point.t) + " · " + Math.round(point.score) + "/100";
    wrap.appendChild(head);

    var rows = document.createElement("div");
    rows.className = "msq-tip-rows";
    [
      "TTFT: " + fmtMs(point.ttft_ms),
      "Total: " + fmtMs(point.total_ms),
      "Tokens: " + (point.completion_tokens || 0),
      "Tok/s: " + (point.tokens_per_second ? point.tokens_per_second.toFixed(1) : "—")
    ].forEach(function (line) {
      var row = document.createElement("div");
      row.textContent = line;
      rows.appendChild(row);
    });
    wrap.appendChild(rows);

    if (point.error) {
      var err = document.createElement("div");
      err.className = "msq-tip-error";
      err.textContent = point.error;
      wrap.appendChild(err);
    }

    if (point.penalties && point.penalties.length) {
      var list = document.createElement("ul");
      list.className = "msq-tip-penalties";
      point.penalties.forEach(function (penalty) {
        var item = document.createElement("li");
        var prefix = penalty.passed === false ? "✗ " : (penalty.passed ? "✓ " : "");
        var points = penalty.points ? " (−" + penalty.points + ")" : "";
        item.textContent = prefix + penalty.reason + points;
        if (penalty.passed) {
          item.className = "is-pass";
        }
        list.appendChild(item);
      });
      wrap.appendChild(list);
    } else if (!point.error) {
      var clean = document.createElement("div");
      clean.className = "msq-tip-clean";
      clean.textContent = "No penalties.";
      wrap.appendChild(clean);
    }

    return wrap;
  }

  function renderChart(container, points, threshold) {
    var svg = container.querySelector(".msq-chart-svg");
    if (!svg || !points.length) {
      return;
    }

    var innerW = WIDTH - PAD * 2;
    var count = points.length;
    function xFor(index) {
      if (count === 1) {
        return PAD + innerW / 2;
      }
      return PAD + (index / (count - 1)) * innerW;
    }

    if (threshold >= 0 && threshold <= 100) {
      svg.appendChild(
        el("line", {
          x1: PAD,
          y1: scoreY(threshold),
          x2: WIDTH - PAD,
          y2: scoreY(threshold),
          class: "msq-threshold"
        })
      );
    }

    var line = "";
    var area = "M " + xFor(0) + " " + (HEIGHT - PAD);
    points.forEach(function (point, index) {
      var x = xFor(index);
      var y = scoreY(point.score);
      line += (index === 0 ? "M " : "L ") + x + " " + y + " ";
      area += " L " + x + " " + y;
    });
    area += " L " + xFor(count - 1) + " " + (HEIGHT - PAD) + " Z";

    svg.appendChild(el("path", { d: area, class: "msq-area" }));
    svg.appendChild(el("path", { d: line.trim(), class: "msq-line" }));

    points.forEach(function (point, index) {
      svg.appendChild(
        el("circle", {
          cx: xFor(index),
          cy: scoreY(point.score),
          r: 2.4,
          class: "msq-dot" + (point.ok ? "" : " is-error")
        })
      );
    });

    var tip = container.querySelector(".msq-tip");
    if (!tip) {
      return;
    }

    function show(event) {
      var rect = svg.getBoundingClientRect();
      if (!rect.width) {
        return;
      }
      var fraction = (event.clientX - rect.left) / rect.width;
      var index = Math.round(fraction * (count - 1));
      index = Math.max(0, Math.min(count - 1, index));
      tip.textContent = "";
      tip.appendChild(buildTooltip(points[index]));
      tip.hidden = false;
      var tipWidth = tip.offsetWidth || 180;
      var offset = (xFor(index) / WIDTH) * rect.width;
      var left = Math.max(0, Math.min(rect.width - tipWidth, offset - tipWidth / 2));
      tip.style.left = left + "px";
    }

    container.addEventListener("mousemove", show);
    container.addEventListener("mouseleave", function () {
      tip.hidden = true;
    });
  }

  var dataScript = document.getElementById("msq-data");
  if (dataScript) {
    var charts = {};
    try {
      charts = JSON.parse(dataScript.textContent || "{}");
    } catch (error) {
      charts = {};
    }
    document.querySelectorAll(".msq-chart").forEach(function (container) {
      var id = container.getAttribute("data-recorder");
      var threshold = parseFloat(container.getAttribute("data-threshold"));
      renderChart(container, charts[id] || [], isNaN(threshold) ? -1 : threshold);
    });
  }

  var form = document.getElementById("msq-form");
  if (!form) {
    return;
  }

  var nameInput = document.getElementById("msq-name");
  var routeSelect = document.getElementById("msq-route");
  var modelInput = document.getElementById("msq-model");
  var modelList = document.getElementById("msq-models");
  var nameTouched = Boolean(nameInput.value.trim());

  function routeName() {
    if (!routeSelect || routeSelect.selectedIndex < 0) {
      return "";
    }
    var option = routeSelect.options[routeSelect.selectedIndex];
    return option && option.value ? option.textContent.trim() : "";
  }

  function autoName() {
    if (nameTouched) {
      return;
    }
    var route = routeName();
    var model = modelInput.value.trim();
    if (route && model) {
      nameInput.value = route + " - " + model;
    }
  }

  nameInput.addEventListener("input", function () {
    nameTouched = nameInput.value.trim().length > 0;
  });

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
    routeSelect.addEventListener("change", function () {
      filterModels();
      autoName();
    });
    filterModels();
  }
  modelInput.addEventListener("input", autoName);
  modelInput.addEventListener("change", autoName);

  var collectRequests = form.querySelector('input[name="collect_requests"]');
  var collectNeedle = document.getElementById("msq-collect-needle");
  function updateCollectNeedle() {
    if (!collectNeedle) {
      return;
    }
    collectNeedle.hidden = !(collectRequests && collectRequests.checked);
  }
  if (collectRequests) {
    collectRequests.addEventListener("change", updateCollectNeedle);
  }
  updateCollectNeedle();
})();
