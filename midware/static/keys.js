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

  function syncProviders() {
    if (!restrictProviders || !providerPanel) {
      return;
    }
    providerPanel.hidden = !restrictProviders.checked;
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
    restrictProviders.addEventListener("change", syncProviders);
  }

  syncPremodels();
  syncProviders();
})();
