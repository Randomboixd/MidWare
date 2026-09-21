(function () {
  "use strict";

  var form = document.getElementById("key-form");
  if (!form) {
    return;
  }

  var toggle = document.getElementById("advanced-toggle");
  var panel = document.getElementById("advanced-panel");
  var allowPremodels = document.getElementById("allow-premodels");
  var restrictPremodels = document.getElementById("restrict-premodels");
  var premodelPanel = document.getElementById("premodel-panel");
  var restrictProviders = document.getElementById("restrict-providers");
  var providerPanel = document.getElementById("provider-panel");
  var restrictModels = document.getElementById("restrict-models");
  var modelPanel = document.getElementById("model-panel");
  var enableAllModels = document.getElementById("models-enable-all");
  var disableAllModels = document.getElementById("models-disable-all");

  var routeBoxes = Array.prototype.slice.call(
    form.querySelectorAll('input[name="allowed_route_ids"]')
  );
  var modelRows = Array.prototype.slice.call(form.querySelectorAll("[data-model-route]"));

  function syncPremodels() {
    if (!allowPremodels || !restrictPremodels || !premodelPanel) {
      return;
    }
    var enabled = allowPremodels.checked;
    restrictPremodels.disabled = !enabled;
    if (!enabled) {
      restrictPremodels.checked = false;
    }
    premodelPanel.hidden = !(enabled && restrictPremodels.checked);
  }

  function routeEnabled(routeId) {
    if (!restrictProviders || !restrictProviders.checked) {
      return true;
    }
    return routeBoxes.some(function (box) {
      return box.value === routeId && box.checked;
    });
  }

  function syncModels() {
    modelRows.forEach(function (row) {
      row.hidden = !routeEnabled(row.getAttribute("data-model-route"));
    });
    if (modelPanel && restrictModels) {
      modelPanel.hidden = !restrictModels.checked;
    }
  }

  function setVisibleModels(checked) {
    modelRows.forEach(function (row) {
      if (row.hidden) {
        return;
      }
      var box = row.querySelector('input[name="allowed_model_ids"]');
      if (box) {
        box.checked = checked;
      }
    });
  }

  if (toggle && panel) {
    toggle.addEventListener("click", function () {
      panel.hidden = false;
      toggle.hidden = true;
    });
  }
  if (allowPremodels) {
    allowPremodels.addEventListener("change", syncPremodels);
  }
  if (restrictPremodels) {
    restrictPremodels.addEventListener("change", syncPremodels);
  }
  if (restrictProviders) {
    restrictProviders.addEventListener("change", function () {
      if (providerPanel) {
        providerPanel.hidden = !restrictProviders.checked;
      }
      syncModels();
    });
  }
  routeBoxes.forEach(function (box) {
    box.addEventListener("change", syncModels);
  });
  if (restrictModels) {
    restrictModels.addEventListener("change", syncModels);
  }
  if (enableAllModels) {
    enableAllModels.addEventListener("click", function () {
      setVisibleModels(true);
    });
  }
  if (disableAllModels) {
    disableAllModels.addEventListener("click", function () {
      setVisibleModels(false);
    });
  }

  syncPremodels();
  if (providerPanel && restrictProviders) {
    providerPanel.hidden = !restrictProviders.checked;
  }
  syncModels();
})();
